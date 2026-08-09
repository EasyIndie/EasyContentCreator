from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest

from apps.common.database import Database
from apps.worker.generation import FactCardGenerationHandler
from apps.worker.main import PollingWorker
from packages.domain import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    ContentProject,
    ProjectRepository,
    ProjectStatus,
    Source,
    SourceExcerpt,
    SourceKind,
    SourceRepository,
)
from packages.pipeline import (
    FactCardGenerationSpec,
    GenerationRequestRepository,
    GenerationTerminalRepository,
    Job,
    JobStatus,
    JobStore,
    LostLeaseError,
    citations_for_sources,
)
from packages.providers import FakeProvider

NOW = datetime.now(UTC) + timedelta(minutes=1)
LEASE = timedelta(seconds=10)


def make_source() -> Source:
    return Source(
        uuid4(),
        SourceKind.OFFICIAL_DOCUMENTATION,
        "Official",
        "https://example.test/source",
        NOW,
        "a" * 64,
        "Verified source",
        (SourceExcerpt(uuid4(), "Evidence", "section-1"),),
    )


def prepare(database: Database) -> tuple[ContentProject, Source, object]:
    project = ContentProject(uuid4(), "Atomic terminal", ProjectStatus.DRAFT, NOW, NOW)
    source = make_source()
    ProjectRepository(database).create(project)
    SourceRepository(database).add(source)
    reservation = GenerationRequestRepository(database).reserve_and_enqueue(
        FactCardGenerationSpec(project.id, (source.id,), "fact-card-v1", 1000),
        "request-1",
        NOW,
        3,
    )
    return project, source, reservation


def claim(database: Database, now: datetime = NOW):
    job = JobStore(database).claim("worker-1", now, LEASE)
    assert job is not None
    return job


def artifact_for(project: ContentProject, source: Source, reservation: object) -> Artifact:
    artifact_id = reservation.artifact_id  # type: ignore[attr-defined]
    version = reservation.artifact_version  # type: ignore[attr-defined]
    return Artifact(
        ArtifactRef(artifact_id, version, ArtifactKind.FACT_CARD, "b" * 64),
        project.id,
        f"facts/{artifact_id}-v{version}.json",
        NOW,
        "test",
        "fake",
        citations=citations_for_sources((source,)),
    )


def read_job(database: Database, job_id: UUID) -> tuple[str, str | None]:
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT status, last_error FROM jobs WHERE id=%s", (job_id,))
        row = cursor.fetchone()
    assert row is not None
    return row


def artifact_count(database: Database) -> int:
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM artifacts")
        return cursor.fetchone()[0]


def test_success_terminal_is_atomic_and_cannot_be_reclaimed(database: Database) -> None:
    project, source, reservation = prepare(database)
    job = claim(database)
    artifact = artifact_for(project, source, reservation)

    GenerationTerminalRepository(database).succeed(
        job.id, "worker-1", NOW, artifact, {source.id: source}
    )

    assert read_job(database, job.id) == (JobStatus.SUCCEEDED.value, None)
    stored = ProjectRepository(database).get(project.id)
    assert stored.status is ProjectStatus.REVIEW_REQUIRED
    assert stored.current_artifacts[ArtifactKind.FACT_CARD] == artifact.ref
    assert artifact_count(database) == 1
    assert JobStore(database).claim("worker-2", NOW + LEASE, LEASE) is None
    with pytest.raises(LostLeaseError):
        GenerationTerminalRepository(database).succeed(
            job.id, "worker-1", NOW, artifact, {source.id: source}
        )
    assert artifact_count(database) == 1


def test_permanent_evidence_failure_saves_candidate_without_pointer(database: Database) -> None:
    project, source, reservation = prepare(database)
    job = claim(database)
    candidate = replace(artifact_for(project, source, reservation), citations=())

    GenerationTerminalRepository(database).fail_permanently(
        job.id, "worker-1", NOW, "EvidenceSourceError", candidate
    )

    assert read_job(database, job.id) == (JobStatus.FAILED.value, "EvidenceSourceError")
    stored = ProjectRepository(database).get(project.id)
    assert stored.status is ProjectStatus.FAILED
    assert stored.current_artifacts == {}
    assert artifact_count(database) == 1


def test_terminal_injected_failure_rolls_back_every_write(database: Database) -> None:
    project, source, reservation = prepare(database)
    job = claim(database)
    artifact = artifact_for(project, source, reservation)
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """CREATE FUNCTION fail_terminal_project_update() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'terminal injected'; END; $$"""
        )
        cursor.execute(
            """CREATE TRIGGER terminal_project_failure BEFORE UPDATE ON projects
            FOR EACH ROW EXECUTE FUNCTION fail_terminal_project_update()"""
        )
    try:
        with pytest.raises(psycopg.DatabaseError, match="terminal injected"):
            GenerationTerminalRepository(database).succeed(
                job.id, "worker-1", NOW, artifact, {source.id: source}
            )
    finally:
        with database.connect() as connection, connection.cursor() as cursor:
            cursor.execute("DROP TRIGGER terminal_project_failure ON projects")
            cursor.execute("DROP FUNCTION fail_terminal_project_update()")
    assert artifact_count(database) == 0
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.GENERATING
    assert read_job(database, job.id)[0] == JobStatus.RUNNING.value


def test_lost_lease_and_precommit_crash_leave_no_terminal_writes(database: Database) -> None:
    project, source, reservation = prepare(database)
    job = claim(database)
    artifact = artifact_for(project, source, reservation)
    recovered_at = NOW + LEASE
    recovered = JobStore(database).claim("worker-2", recovered_at, LEASE)
    assert recovered is not None

    with pytest.raises(LostLeaseError):
        GenerationTerminalRepository(database).succeed(
            job.id, "worker-1", recovered_at, artifact, {source.id: source}
        )
    assert artifact_count(database) == 0
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.GENERATING
    assert recovered.attempt == 2


def test_retryable_handler_only_requeues_original_reservation(
    database: Database, tmp_path: Path
) -> None:
    project, _source, reservation = prepare(database)
    current_time = {"value": NOW}
    handler = FactCardGenerationHandler(
        database,
        FakeProvider(
            created_at=NOW,
            created_by="worker",
            storage_root=tmp_path,
            failure="retryable_once",
        ),
        lambda: current_time["value"],
    )
    worker = PollingWorker(
        database,
        1,
        handlers={"generate_fact_card": handler},
        worker_id="worker-1",
        lease_for=LEASE,
        clock=lambda: current_time["value"],
    )

    worker.poll_once()

    assert read_job(database, reservation.job_id)[0] == JobStatus.QUEUED.value
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.GENERATING
    assert artifact_count(database) == 0
    loaded = GenerationRequestRepository(database).get_by_job_id(reservation.job_id)
    assert loaded == reservation

    current_time["value"] = NOW + timedelta(seconds=1)
    worker.poll_once()

    assert read_job(database, reservation.job_id)[0] == JobStatus.SUCCEEDED.value
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.REVIEW_REQUIRED
    assert artifact_count(database) == 1


def test_handler_return_without_terminal_fails_closed(database: Database) -> None:
    project, _source, reservation = prepare(database)
    worker = PollingWorker(
        database,
        1,
        handlers={"generate_fact_card": lambda _job: None},
        worker_id="worker-1",
        lease_for=LEASE,
        clock=lambda: NOW,
    )

    worker.poll_once()

    assert read_job(database, reservation.job_id) == (
        JobStatus.FAILED.value,
        "AdapterContractError",
    )
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.GENERATING


def test_handler_crash_before_terminal_waits_for_lease_recovery(database: Database) -> None:
    project, _source, reservation = prepare(database)

    def crash(_job: Job) -> None:
        raise RuntimeError("simulated process crash")

    worker = PollingWorker(
        database,
        1,
        handlers={"generate_fact_card": crash},
        worker_id="worker-1",
        lease_for=LEASE,
        clock=lambda: NOW,
    )

    worker.poll_once()

    assert read_job(database, reservation.job_id)[0] == JobStatus.RUNNING.value
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.GENERATING
    recovered = JobStore(database).claim("worker-2", NOW + LEASE, LEASE)
    assert recovered is not None
    assert recovered.id == reservation.job_id

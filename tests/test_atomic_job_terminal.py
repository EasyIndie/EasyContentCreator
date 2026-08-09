import logging
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
    ArtifactRepository,
    ContentProject,
    InvalidStateTransition,
    ProjectRepository,
    ProjectStatus,
    Source,
    SourceExcerpt,
    SourceKind,
    SourceRepository,
)
from packages.pipeline import (
    EvidenceSourceError,
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


def test_success_artifact_round_trip_preserves_citations_metadata_and_lineage(
    database: Database,
) -> None:
    project, source, reservation = prepare(database)
    upstream = Artifact(
        ArtifactRef(uuid4(), 1, ArtifactKind.SCRIPT, "c" * 64),
        project.id,
        "inputs/script.json",
        NOW,
        "fixture",
        "fixture@1",
        metadata={"input": "stable"},
    )
    ArtifactRepository(database).add(upstream)
    job = claim(database)
    artifact = replace(
        artifact_for(project, source, reservation),
        upstream=(upstream.ref,),
        metadata={"nested": {"value": 1}, "labels": ("fact", "card")},
    )

    GenerationTerminalRepository(database).succeed(
        job.id, "worker-1", NOW, artifact, {source.id: source}
    )

    assert (
        ArtifactRepository(database).get(artifact.ref.artifact_id, artifact.ref.version) == artifact
    )


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


def test_permanent_failure_keeps_existing_current_pointer(database: Database) -> None:
    project = ContentProject(uuid4(), "Existing pointer", ProjectStatus.DRAFT, NOW, NOW)
    source = make_source()
    ProjectRepository(database).create(project)
    SourceRepository(database).add(source)
    previous = Artifact(
        ArtifactRef(uuid4(), 1, ArtifactKind.FACT_CARD, "d" * 64),
        project.id,
        "facts/previous.json",
        NOW,
        "fixture",
        "fixture@1",
        citations=citations_for_sources((source,)),
    )
    ArtifactRepository(database).add(previous)
    pointed = replace(
        project,
        revision=2,
        current_artifacts={ArtifactKind.FACT_CARD: previous.ref},
    )
    ProjectRepository(database).update(pointed, expected_revision=1)
    reservation = GenerationRequestRepository(database).reserve_and_enqueue(
        FactCardGenerationSpec(project.id, (source.id,), "fact-card-v1", 1000),
        "replacement",
        NOW,
        3,
    )
    job = claim(database)
    candidate = replace(artifact_for(project, source, reservation), citations=())

    GenerationTerminalRepository(database).fail_permanently(
        job.id, "worker-1", NOW, "EvidenceSourceError", candidate
    )

    stored = ProjectRepository(database).get(project.id)
    assert stored.status is ProjectStatus.FAILED
    assert stored.current_artifacts[ArtifactKind.FACT_CARD] == previous.ref


@pytest.mark.parametrize(
    "error_class",
    ("", "Permanent Error", "secret: provider body", "A" * 129, "Error\nsecret"),
)
def test_permanent_failure_rejects_unsafe_error_classes(
    database: Database, error_class: str
) -> None:
    _project, _source, _reservation = prepare(database)
    job = claim(database)

    with pytest.raises(ValueError, match="safe exception class"):
        GenerationTerminalRepository(database).fail_permanently(
            job.id, "worker-1", NOW, error_class
        )

    assert read_job(database, job.id)[0] == JobStatus.RUNNING.value


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


def test_permanent_terminal_failure_rolls_back_candidate_project_and_job(
    database: Database,
) -> None:
    project, source, reservation = prepare(database)
    job = claim(database)
    candidate = replace(artifact_for(project, source, reservation), citations=())
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """CREATE FUNCTION fail_permanent_project_update() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'permanent injected'; END; $$"""
        )
        cursor.execute(
            """CREATE TRIGGER permanent_project_failure BEFORE UPDATE ON projects
            FOR EACH ROW EXECUTE FUNCTION fail_permanent_project_update()"""
        )
    try:
        with pytest.raises(psycopg.DatabaseError, match="permanent injected"):
            GenerationTerminalRepository(database).fail_permanently(
                job.id, "worker-1", NOW, "EvidenceSourceError", candidate
            )
    finally:
        with database.connect() as connection, connection.cursor() as cursor:
            cursor.execute("DROP TRIGGER permanent_project_failure ON projects")
            cursor.execute("DROP FUNCTION fail_permanent_project_update()")
    assert artifact_count(database) == 0
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.GENERATING
    assert read_job(database, job.id)[0] == JobStatus.RUNNING.value


def test_project_revision_mismatch_rolls_back_terminal(database: Database) -> None:
    project, source, reservation = prepare(database)
    job = claim(database)
    artifact = artifact_for(project, source, reservation)
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE projects SET revision=revision+1 WHERE id=%s", (project.id,))

    with pytest.raises(InvalidStateTransition, match="reservation"):
        GenerationTerminalRepository(database).succeed(
            job.id, "worker-1", NOW, artifact, {source.id: source}
        )

    assert artifact_count(database) == 0
    assert read_job(database, job.id)[0] == JobStatus.RUNNING.value


def test_success_rejects_missing_extra_or_replaced_reserved_sources(
    database: Database,
) -> None:
    project = ContentProject(uuid4(), "Exact sources", ProjectStatus.DRAFT, NOW, NOW)
    source_a = make_source()
    source_b = make_source()
    source_c = make_source()
    ProjectRepository(database).create(project)
    source_repository = SourceRepository(database)
    for source in (source_a, source_b, source_c):
        source_repository.add(source)
    reservation = GenerationRequestRepository(database).reserve_and_enqueue(
        FactCardGenerationSpec(
            project.id,
            (source_a.id, source_b.id),
            "fact-card-v1",
            1000,
        ),
        "exact-sources",
        NOW,
        3,
    )
    job = claim(database)
    base = artifact_for(project, source_a, reservation)
    terminal = GenerationTerminalRepository(database)
    cases = (
        (
            replace(base, citations=citations_for_sources((source_a,))),
            {source_a.id: source_a},
        ),
        (
            replace(base, citations=citations_for_sources((source_a, source_b, source_c))),
            {source_a.id: source_a, source_b.id: source_b, source_c.id: source_c},
        ),
        (
            replace(base, citations=citations_for_sources((source_a, source_c))),
            {source_a.id: source_a, source_c.id: source_c},
        ),
    )

    for artifact, sources in cases:
        with pytest.raises(EvidenceSourceError, match="sources do not exactly match"):
            terminal.succeed(job.id, "worker-1", NOW, artifact, sources)
        assert artifact_count(database) == 0
        assert ProjectRepository(database).get(project.id).status is ProjectStatus.GENERATING
        assert read_job(database, job.id)[0] == JobStatus.RUNNING.value

    with pytest.raises(EvidenceSourceError, match="citations do not match"):
        terminal.succeed(
            job.id,
            "worker-1",
            NOW,
            replace(base, citations=citations_for_sources((source_a,))),
            {source_a.id: source_a, source_b.id: source_b},
        )
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


def test_handler_permanent_provider_failure_is_atomic(database: Database, tmp_path: Path) -> None:
    project, _source, reservation = prepare(database)
    handler = FactCardGenerationHandler(
        database,
        FakeProvider(
            created_at=NOW,
            created_by="worker",
            storage_root=tmp_path,
            failure="permanent",
        ),
        lambda: NOW,
    )
    worker = PollingWorker(
        database,
        1,
        handlers={"generate_fact_card": handler},
        worker_id="worker-1",
        lease_for=LEASE,
        clock=lambda: NOW,
    )

    worker.poll_once()

    assert read_job(database, reservation.job_id) == (
        JobStatus.FAILED.value,
        "PermanentError",
    )
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.FAILED


@pytest.mark.parametrize("source_mode", ("missing", "empty"))
def test_handler_invalid_reserved_source_fails_project_and_job(
    database: Database, tmp_path: Path, source_mode: str
) -> None:
    project = ContentProject(uuid4(), "Invalid source", ProjectStatus.DRAFT, NOW, NOW)
    ProjectRepository(database).create(project)
    source_id = uuid4()
    if source_mode == "empty":
        empty = replace(make_source(), id=source_id, excerpts=())
        SourceRepository(database).add(empty)
    reservation = GenerationRequestRepository(database).reserve_and_enqueue(
        FactCardGenerationSpec(project.id, (source_id,), "fact-card-v1", 1000),
        f"source-{source_mode}",
        NOW,
        3,
    )
    handler = FactCardGenerationHandler(
        database,
        FakeProvider(created_at=NOW, created_by="worker", storage_root=tmp_path),
        lambda: NOW,
    )
    worker = PollingWorker(
        database,
        1,
        handlers={"generate_fact_card": handler},
        worker_id="worker-1",
        lease_for=LEASE,
        clock=lambda: NOW,
    )

    worker.poll_once()

    expected_error = "EntityNotFoundError" if source_mode == "missing" else "EvidenceSourceError"
    assert read_job(database, reservation.job_id) == (JobStatus.FAILED.value, expected_error)
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.FAILED
    assert artifact_count(database) == 0


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


def test_handler_exception_after_terminal_does_not_corrupt_success_or_leak_logs(
    database: Database,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project, source, reservation = prepare(database)
    delegate = FactCardGenerationHandler(
        database,
        FakeProvider(created_at=NOW, created_by="worker", storage_root=tmp_path),
        lambda: NOW,
    )
    secret = "sk-do-not-log-this"
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE jobs SET payload=%s WHERE id=%s",
            (psycopg.types.json.Jsonb({"secret": secret}), reservation.job_id),
        )

    def commit_then_crash(job: Job) -> None:
        delegate(job)
        raise RuntimeError(f"provider body contains {secret} and {source.excerpts[0].text}")

    caplog.set_level(logging.WARNING, logger="apps.worker.main")
    worker = PollingWorker(
        database,
        1,
        handlers={"generate_fact_card": commit_then_crash},
        worker_id="worker-1",
        lease_for=LEASE,
        clock=lambda: NOW,
    )

    worker.poll_once()

    assert read_job(database, reservation.job_id) == (JobStatus.SUCCEEDED.value, None)
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.REVIEW_REQUIRED
    rendered_logs = caplog.text
    assert secret not in rendered_logs
    assert source.excerpts[0].text not in rendered_logs
    assert "provider body" not in rendered_logs
    assert "source_ids" not in rendered_logs
    assert caplog.records[-1].error_class == "RuntimeError"  # type: ignore[attr-defined]

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app, get_database, utc_now
from apps.common.database import Database
from apps.worker.generation import FactCardGenerationHandler
from apps.worker.main import PollingWorker
from packages.domain import (
    ArtifactKind,
    ArtifactRepository,
    ContentProject,
    ProjectRepository,
    ProjectStatus,
    Source,
    SourceExcerpt,
    SourceKind,
    SourceRepository,
)
from packages.pipeline import GenerationRequestRepository, JobStatus, JobStore
from packages.providers import FakeProvider, GenerationRequest

NOW = datetime.now(UTC) + timedelta(minutes=1)


@pytest.fixture
def api(database: Database) -> Iterator[tuple[TestClient, dict[str, datetime]]]:
    clock = {"now": NOW}
    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[utc_now] = lambda: clock["now"]
    try:
        yield TestClient(app), clock
    finally:
        app.dependency_overrides.clear()


def add_project(database: Database, title: str = "M1 vertical slice") -> ContentProject:
    project = ContentProject(uuid4(), title, ProjectStatus.DRAFT, NOW, NOW)
    ProjectRepository(database).create(project)
    return project


def add_source(database: Database, *, excerpts: bool = True) -> Source:
    source = Source(
        uuid4(),
        SourceKind.OFFICIAL_DOCUMENTATION,
        "Official source",
        "https://example.test/official",
        NOW,
        "a" * 64,
        "Trusted summary",
        (SourceExcerpt(uuid4(), "Verified evidence", "section-1"),) if excerpts else (),
    )
    SourceRepository(database).add(source)
    return source


def request_body(*source_ids: UUID, budget_units: int = 1000) -> dict[str, object]:
    return {
        "source_ids": [str(item) for item in source_ids],
        "template_version": "fact-card-v1",
        "budget_units": budget_units,
    }


def worker(
    database: Database,
    artifact_root: Path,
    clock: dict[str, datetime],
    *,
    failure: str | None = None,
    worker_id: str = "worker-e2e",
) -> PollingWorker:
    provider = FakeProvider(
        created_at=NOW,
        created_by="e2e-worker",
        storage_root=artifact_root,
        failure=failure,  # type: ignore[arg-type]
    )
    return PollingWorker(
        database,
        poll_interval_seconds=1,
        handlers={
            "generate_fact_card": FactCardGenerationHandler(
                database, provider, lambda: clock["now"]
            )
        },
        worker_id=worker_id,
        lease_for=timedelta(seconds=10),
        clock=lambda: clock["now"],
    )


def read_job_status(database: Database, job_id: UUID) -> str:
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT status FROM jobs WHERE id=%s", (job_id,))
        row = cursor.fetchone()
    assert row is not None
    return row[0]


def test_generate_api_idempotency_and_validation(
    database: Database,
    api: tuple[TestClient, dict[str, datetime]],
) -> None:
    client, _clock = api
    project = add_project(database)
    source = add_source(database)
    url = f"/projects/{project.id}/generate"

    first = client.post(url, headers={"Idempotency-Key": "request-1"}, json=request_body(source.id))
    replay = client.post(
        url, headers={"Idempotency-Key": "request-1"}, json=request_body(source.id)
    )
    conflict = client.post(
        url,
        headers={"Idempotency-Key": "request-1"},
        json=request_body(source.id, budget_units=1001),
    )

    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert first.json()["status"] == "queued"
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM generation_requests")
        assert cursor.fetchone() == (1,)
        cursor.execute("SELECT COUNT(*) FROM jobs")
        assert cursor.fetchone() == (1,)

    invalid_requests = (
        ({}, request_body(source.id)),
        ({"Idempotency-Key": "   "}, request_body(source.id)),
        ({"Idempotency-Key": "x" * 201}, request_body(source.id)),
        ({"Idempotency-Key": "bad-empty"}, request_body()),
        ({"Idempotency-Key": "bad-duplicate"}, request_body(source.id, source.id)),
        (
            {"Idempotency-Key": "bad-template"},
            {**request_body(source.id), "template_version": " "},
        ),
        ({"Idempotency-Key": "bad-budget"}, request_body(source.id, budget_units=0)),
    )
    for headers, body in invalid_requests:
        assert client.post(url, headers=headers, json=body).status_code == 422


def test_fake_success_reaches_approved_with_reproducible_evidence(
    database: Database,
    api: tuple[TestClient, dict[str, datetime]],
    tmp_path: Path,
) -> None:
    client, clock = api
    project = add_project(database)
    source = add_source(database)
    response = client.post(
        f"/projects/{project.id}/generate",
        headers={"Idempotency-Key": "success"},
        json=request_body(source.id),
    )
    job_id = UUID(response.json()["job_id"])

    worker(database, tmp_path / "first", clock).poll_once()

    generated = ProjectRepository(database).get(project.id)
    assert generated.status is ProjectStatus.REVIEW_REQUIRED
    ref = generated.current_artifacts[ArtifactKind.FACT_CARD]
    artifact = ArtifactRepository(database).get(ref.artifact_id, ref.version)
    assert artifact.citations[0].source_id == source.id
    assert artifact.citations[0].excerpt_id == source.excerpts[0].id
    assert artifact.citations[0].source_sha256 == source.sha256
    assert read_job_status(database, job_id) == JobStatus.SUCCEEDED.value

    reservation = GenerationRequestRepository(database).get_by_job_id(job_id)
    second_root = tmp_path / "second"
    reproduced = FakeProvider(
        created_at=NOW,
        created_by="e2e-worker",
        storage_root=second_root,
    ).generate(
        GenerationRequest(
            project_id=project.id,
            capability="fact_card",
            output_kind=ArtifactKind.FACT_CARD,
            output_artifact_id=reservation.artifact_id,
            output_version=reservation.artifact_version,
            inputs=(),
            template_version=reservation.spec.template_version,
            budget_units=reservation.spec.budget_units,
            parameters={"source_ids": (str(source.id),)},
        )
    )
    assert (tmp_path / "first" / artifact.storage_path).read_bytes() == (
        second_root / reproduced.artifact.storage_path
    ).read_bytes()

    approved = client.post(
        f"/projects/{project.id}/reviews",
        json={
            "decision": "approve",
            "note": "Evidence verified",
            "expected_revision": generated.revision,
        },
    )
    assert approved.status_code == 201
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.APPROVED


def test_retryable_failure_reuses_job_and_reservation_then_succeeds(
    database: Database,
    api: tuple[TestClient, dict[str, datetime]],
    tmp_path: Path,
) -> None:
    client, clock = api
    project = add_project(database)
    source = add_source(database)
    response = client.post(
        f"/projects/{project.id}/generate",
        headers={"Idempotency-Key": "retry"},
        json=request_body(source.id),
    )
    job_id = UUID(response.json()["job_id"])
    reservation = GenerationRequestRepository(database).get_by_job_id(job_id)
    retrying_worker = worker(
        database, tmp_path, clock, failure="retryable_once", worker_id="retry-worker"
    )

    retrying_worker.poll_once()
    assert read_job_status(database, job_id) == JobStatus.QUEUED.value
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.GENERATING

    clock["now"] += timedelta(seconds=1)
    retrying_worker.poll_once()
    assert read_job_status(database, job_id) == JobStatus.SUCCEEDED.value
    assert GenerationRequestRepository(database).get_by_job_id(job_id) == reservation
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.REVIEW_REQUIRED


def test_permanent_evidence_failure_recovers_with_new_version(
    database: Database,
    api: tuple[TestClient, dict[str, datetime]],
    tmp_path: Path,
) -> None:
    client, clock = api
    project = add_project(database)
    invalid = add_source(database, excerpts=False)
    first = client.post(
        f"/projects/{project.id}/generate",
        headers={"Idempotency-Key": "invalid-evidence"},
        json=request_body(invalid.id),
    )
    first_job = UUID(first.json()["job_id"])
    worker(database, tmp_path, clock).poll_once()
    assert read_job_status(database, first_job) == JobStatus.FAILED.value
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.FAILED

    valid = add_source(database)
    clock["now"] += timedelta(seconds=1)
    second = client.post(
        f"/projects/{project.id}/generate",
        headers={"Idempotency-Key": "recovery"},
        json=request_body(valid.id),
    )
    second_job = UUID(second.json()["job_id"])
    second_reservation = GenerationRequestRepository(database).get_by_job_id(second_job)
    assert second_reservation.artifact_version == 2
    worker(database, tmp_path, clock, worker_id="recovery-worker").poll_once()
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.REVIEW_REQUIRED


def test_process_restart_recovers_expired_lease_without_duplicate_artifact(
    database: Database,
    api: tuple[TestClient, dict[str, datetime]],
    tmp_path: Path,
) -> None:
    client, clock = api
    project = add_project(database)
    source = add_source(database)
    response = client.post(
        f"/projects/{project.id}/generate",
        headers={"Idempotency-Key": "restart"},
        json=request_body(source.id),
    )
    job_id = UUID(response.json()["job_id"])
    claimed = JobStore(database).claim("dead-worker", clock["now"], timedelta(seconds=5))
    assert claimed is not None

    clock["now"] += timedelta(seconds=5)
    worker(database, tmp_path, clock, worker_id="restarted-worker").poll_once()

    assert read_job_status(database, job_id) == JobStatus.SUCCEEDED.value
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.REVIEW_REQUIRED
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM artifacts WHERE project_id=%s", (project.id,))
        assert cursor.fetchone() == (1,)

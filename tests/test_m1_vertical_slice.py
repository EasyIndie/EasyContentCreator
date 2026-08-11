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
from packages.pipeline import (
    FactCardGenerationSpec,
    GenerationRequestRepository,
    JobStatus,
    JobStore,
)
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


def add_source(
    database: Database, *, excerpts: bool = True, excerpt_count: int = 1, sha256: str = "a" * 64
) -> Source:
    source = Source(
        uuid4(),
        SourceKind.OFFICIAL_DOCUMENTATION,
        "Official source",
        "https://example.test/official",
        NOW,
        sha256,
        "Trusted summary",
        tuple(
            SourceExcerpt(uuid4(), f"Verified evidence {index}", f"section-{index}")
            for index in range(excerpt_count)
        )
        if excerpts
        else (),
    )
    SourceRepository(database).add(source)
    return source


def request_body(*source_ids: UUID, budget_units: object = 1000) -> dict[str, object]:
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
        ({"Idempotency-Key": "bool-budget"}, request_body(source.id, budget_units=True)),
        ({"Idempotency-Key": "string-budget"}, request_body(source.id, budget_units="1000")),
    )
    for headers, body in invalid_requests:
        assert client.post(url, headers=headers, json=body).status_code == 422

    missing = client.post(
        f"/projects/{uuid4()}/generate",
        headers={"Idempotency-Key": "missing-project"},
        json=request_body(source.id),
    )
    assert missing.status_code == 404
    assert missing.json() == {"detail": {"code": "not_found", "message": "project not found"}}
    invalid_path = client.post(
        "/projects/not-a-uuid/generate",
        headers={"Idempotency-Key": "bad-path"},
        json=request_body(source.id),
    )
    assert invalid_path.status_code == 422
    assert isinstance(invalid_path.json()["detail"], list)
    illegal_project = add_project(database, "Already active")
    GenerationRequestRepository(database).reserve_and_enqueue(
        FactCardGenerationSpec(illegal_project.id, (source.id,), "fact-card-v1", 1000),
        "active-first",
        NOW,
        3,
    )
    illegal = client.post(
        f"/projects/{illegal_project.id}/generate",
        headers={"Idempotency-Key": "active-second"},
        json=request_body(source.id),
    )
    assert illegal.status_code == 409
    assert illegal.json() == {
        "detail": {
            "code": "invalid_transition",
            "message": "operation is not allowed for current project status",
        }
    }


def test_fake_success_reaches_approved_with_reproducible_evidence(
    database: Database,
    api: tuple[TestClient, dict[str, datetime]],
    tmp_path: Path,
) -> None:
    client, clock = api
    created = client.post("/projects", json={"title": "M1 API-created project"})
    assert created.status_code == 201
    project = ProjectRepository(database).get(UUID(created.json()["id"]))
    source_a = add_source(database, excerpt_count=2, sha256="a" * 64)
    source_b = add_source(database, excerpt_count=2, sha256="b" * 64)
    response = client.post(
        f"/projects/{project.id}/generate",
        headers={"Idempotency-Key": "success"},
        json=request_body(source_b.id, source_a.id),
    )
    job_id = UUID(response.json()["job_id"])

    worker(database, tmp_path / "first", clock).poll_once()

    generated = ProjectRepository(database).get(project.id)
    assert generated.status is ProjectStatus.REVIEW_REQUIRED
    ref = generated.current_artifacts[ArtifactKind.FACT_CARD]
    artifact = ArtifactRepository(database).get(ref.artifact_id, ref.version)
    assert len(artifact.citations) == 4
    expected_citations = {
        (source.id, excerpt.id, source.sha256)
        for source in (source_a, source_b)
        for excerpt in source.excerpts
    }
    assert {
        (item.source_id, item.excerpt_id, item.source_sha256) for item in artifact.citations
    } == expected_citations
    assert SourceRepository(database).get(source_a.id) == source_a
    assert SourceRepository(database).get(source_b.id) == source_b
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
            parameters={"source_ids": tuple(str(item) for item in reservation.spec.source_ids)},
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

    replay = client.post(
        f"/projects/{project.id}/generate",
        headers={"Idempotency-Key": "success"},
        json=request_body(source_b.id, source_a.id),
    )
    assert replay.status_code == 202
    assert replay.json()["job_id"] == str(job_id)
    assert replay.json()["status"] == "succeeded"


def test_rejection_preserves_artifact_and_recovers_only_with_new_key(
    database: Database,
    api: tuple[TestClient, dict[str, datetime]],
    tmp_path: Path,
) -> None:
    client, clock = api
    project = add_project(database, "Reject and recover")
    source = add_source(database, excerpt_count=2)
    url = f"/projects/{project.id}/generate"
    body = request_body(source.id)

    first = client.post(url, headers={"Idempotency-Key": "first-version"}, json=body)
    first_job = UUID(first.json()["job_id"])
    first_reservation = GenerationRequestRepository(database).get_by_job_id(first_job)
    worker(database, tmp_path, clock, worker_id="first-worker").poll_once()
    review_required = ProjectRepository(database).get(project.id)
    first_ref = review_required.current_artifacts[ArtifactKind.FACT_CARD]
    first_artifact = ArtifactRepository(database).get(first_ref.artifact_id, first_ref.version)
    first_bytes = (tmp_path / first_artifact.storage_path).read_bytes()

    rejected = client.post(
        f"/projects/{project.id}/reviews",
        json={
            "decision": "reject",
            "note": "Evidence is valid, but the framing needs revision",
            "expected_revision": review_required.revision,
        },
    )

    assert rejected.status_code == 201
    failed = ProjectRepository(database).get(project.id)
    assert failed.status is ProjectStatus.FAILED
    assert failed.failed_stage.value == "generation"
    assert failed.current_artifacts[ArtifactKind.FACT_CARD] == first_ref
    assert (
        ArtifactRepository(database).get(first_ref.artifact_id, first_ref.version) == first_artifact
    )
    assert (tmp_path / first_artifact.storage_path).read_bytes() == first_bytes
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM jobs WHERE project_id = %s", (project.id,))
        assert cursor.fetchone() == (1,)
        cursor.execute(
            "SELECT decision, project_revision FROM reviews WHERE project_id = %s",
            (project.id,),
        )
        assert cursor.fetchall() == [("reject", failed.revision)]

    replay = client.post(url, headers={"Idempotency-Key": "first-version"}, json=body)
    assert replay.status_code == 202
    assert replay.json() == {
        "job_id": str(first_job),
        "project_id": str(project.id),
        "status": "succeeded",
    }
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.FAILED

    conflicting_body = {**body, "template_version": "fact-card-v2"}
    conflict = client.post(
        url,
        headers={"Idempotency-Key": "first-version"},
        json=conflicting_body,
    )
    assert conflict.status_code == 409
    assert conflict.json() == {
        "detail": {
            "code": "idempotency_conflict",
            "message": "Idempotency-Key was already used with a different request",
        }
    }
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.FAILED

    clock["now"] += timedelta(seconds=1)
    recovery = client.post(url, headers={"Idempotency-Key": "second-version"}, json=body)
    second_job = UUID(recovery.json()["job_id"])
    second_reservation = GenerationRequestRepository(database).get_by_job_id(second_job)
    assert second_reservation.artifact_id == first_reservation.artifact_id
    assert second_reservation.artifact_version == first_reservation.artifact_version + 1
    assert ProjectRepository(database).get(project.id).status is ProjectStatus.GENERATING
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT COUNT(*) FROM projects AS p
            WHERE p.id = %s AND p.status = 'generating'
              AND NOT EXISTS (
                SELECT 1 FROM jobs AS j
                WHERE j.project_id = p.id AND j.status IN ('queued', 'running')
              )""",
            (project.id,),
        )
        assert cursor.fetchone() == (0,)
        cursor.execute("SELECT COUNT(*) FROM jobs WHERE project_id = %s", (project.id,))
        assert cursor.fetchone() == (2,)

    worker(database, tmp_path, clock, worker_id="recovery-worker").poll_once()
    recovered = ProjectRepository(database).get(project.id)
    assert recovered.status is ProjectStatus.REVIEW_REQUIRED
    assert recovered.current_artifacts[ArtifactKind.FACT_CARD].version == 2
    assert ArtifactRepository(database).get(first_ref.artifact_id, 1) == first_artifact


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
    first_reservation = GenerationRequestRepository(database).get_by_job_id(first_job)
    worker(database, tmp_path, clock).poll_once()
    assert read_job_status(database, first_job) == JobStatus.FAILED.value
    failed = ProjectRepository(database).get(project.id)
    assert failed.status is ProjectStatus.FAILED
    assert failed.current_artifacts == {}

    valid = add_source(database)
    clock["now"] += timedelta(seconds=1)
    second = client.post(
        f"/projects/{project.id}/generate",
        headers={"Idempotency-Key": "recovery"},
        json=request_body(valid.id),
    )
    second_job = UUID(second.json()["job_id"])
    second_reservation = GenerationRequestRepository(database).get_by_job_id(second_job)
    assert second_reservation.artifact_id == first_reservation.artifact_id
    assert second_reservation.artifact_version == 2
    worker(database, tmp_path, clock, worker_id="recovery-worker").poll_once()
    recovered = ProjectRepository(database).get(project.id)
    assert recovered.status is ProjectStatus.REVIEW_REQUIRED
    assert recovered.current_artifacts[ArtifactKind.FACT_CARD].version == 2
    assert (
        tmp_path / ArtifactRepository(database).get(second_reservation.artifact_id, 2).storage_path
    ).is_file()
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM generation_requests WHERE project_id=%s", (project.id,)
        )
        assert cursor.fetchone() == (2,)
        cursor.execute("SELECT COUNT(*) FROM jobs WHERE project_id=%s", (project.id,))
        assert cursor.fetchone() == (2,)
        cursor.execute("SELECT version FROM artifacts WHERE project_id=%s", (project.id,))
        assert cursor.fetchall() == [(2,)]


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


def test_file_written_before_terminal_failure_is_reused_after_lease_recovery(
    database: Database,
    api: tuple[TestClient, dict[str, datetime]],
    tmp_path: Path,
) -> None:
    client, clock = api
    project = add_project(database)
    source = add_source(database)
    response = client.post(
        f"/projects/{project.id}/generate",
        headers={"Idempotency-Key": "file-before-db"},
        json=request_body(source.id),
    )
    job_id = UUID(response.json()["job_id"])
    reservation = GenerationRequestRepository(database).get_by_job_id(job_id)
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """CREATE FUNCTION fail_slice_terminal() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'slice terminal failure'; END; $$"""
        )
        cursor.execute(
            """CREATE TRIGGER slice_terminal_failure BEFORE UPDATE ON projects
            FOR EACH ROW EXECUTE FUNCTION fail_slice_terminal()"""
        )
    first_worker = worker(database, tmp_path, clock, worker_id="crashing-worker")
    try:
        first_worker.poll_once()
    finally:
        with database.connect() as connection, connection.cursor() as cursor:
            cursor.execute("DROP TRIGGER slice_terminal_failure ON projects")
            cursor.execute("DROP FUNCTION fail_slice_terminal()")

    relative_path = (
        f"fake/{project.id}/fact_card/"
        f"{reservation.artifact_id}-v{reservation.artifact_version}.json"
    )
    artifact_file = tmp_path / relative_path
    first_bytes = artifact_file.read_bytes()
    assert read_job_status(database, job_id) == JobStatus.RUNNING.value
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM artifacts WHERE project_id=%s", (project.id,))
        assert cursor.fetchone() == (0,)

    clock["now"] += timedelta(seconds=10)
    worker(database, tmp_path, clock, worker_id="recovery-worker").poll_once()

    assert artifact_file.read_bytes() == first_bytes
    assert read_job_status(database, job_id) == JobStatus.SUCCEEDED.value
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM artifacts WHERE project_id=%s", (project.id,))
        assert cursor.fetchone() == (1,)

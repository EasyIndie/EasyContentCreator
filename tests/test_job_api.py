from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app, get_database
from apps.common.database import Database
from packages.domain import ContentProject, ProjectRepository, ProjectStatus
from packages.pipeline import FactCardGenerationSpec, GenerationRequestRepository, JobStore

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


@pytest.fixture
def client(database: Database) -> Iterator[TestClient]:
    app.dependency_overrides[get_database] = lambda: database
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def create_project(database: Database, title: str = "Jobs") -> ContentProject:
    project = ContentProject(uuid4(), title, ProjectStatus.DRAFT, NOW, NOW)
    ProjectRepository(database).create(project)
    return project


def set_job(
    database: Database,
    job_id: UUID,
    *,
    status: str,
    attempt: int,
    last_error: str | None = None,
    updated_at: datetime | None = None,
) -> None:
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE jobs SET status=%s, attempt=%s, last_error=%s,
            lease_owner=NULL, lease_expires_at=NULL, updated_at=%s WHERE id=%s""",
            (
                status,
                attempt,
                last_error,
                updated_at or NOW + timedelta(seconds=attempt),
                job_id,
            ),
        )
        assert cursor.rowcount == 1


def test_job_views_are_safe_sorted_and_recoverable_only_for_failed_generation(
    database: Database, client: TestClient
) -> None:
    project = create_project(database)
    other = create_project(database, "Other")
    store = JobStore(database)
    reservation = GenerationRequestRepository(database).reserve_and_enqueue(
        FactCardGenerationSpec(project.id, (uuid4(),), "fact-card-v1", 100),
        "generation",
        NOW,
        3,
    )
    failed = reservation.job_id
    older = store.enqueue("other", project.id, {"secret": "do-not-leak"}, 3, NOW)
    other_job = store.enqueue("other", other.id, {"token": "hidden"}, 1, NOW)
    set_job(database, older, status="succeeded", attempt=1)
    set_job(
        database,
        failed,
        status="failed",
        attempt=3,
        last_error="exception contained secret=top-secret",
    )
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE projects SET status='failed', failed_stage='generation',
            revision=revision+1, updated_at=%s WHERE id=%s""",
            (NOW + timedelta(seconds=5), project.id),
        )

    response = client.get(f"/projects/{project.id}/jobs")

    assert response.status_code == 200
    items = response.json()["items"]
    by_id = {item["id"]: item for item in items}
    assert set(by_id) == {str(failed), str(older)}
    assert by_id[str(failed)]["error_class"] == "UnknownError"
    assert by_id[str(failed)]["recoverable"] is True
    assert by_id[str(older)]["recoverable"] is False
    assert client.get(f"/projects/{project.id}/jobs").json()["items"] == items
    serialized = response.text
    for forbidden in (
        "payload",
        "lease_owner",
        "lease_expires_at",
        "do-not-leak",
        "private evidence",
        "top-secret",
        str(other_job),
    ):
        assert forbidden not in serialized

    detail = client.get(f"/jobs/{failed}")
    assert detail.status_code == 200
    assert detail.json()["error_class"] == "UnknownError"
    assert detail.json()["recoverable"] is True


def test_safe_error_class_and_all_runtime_statuses_round_trip(
    database: Database, client: TestClient
) -> None:
    project = create_project(database)
    store = JobStore(database)
    queued = store.enqueue("queued", project.id, {}, 3, NOW)
    running = store.enqueue("running", project.id, {}, 3, NOW)
    succeeded = store.enqueue("succeeded", project.id, {}, 3, NOW)
    failed = store.enqueue("failed", project.id, {}, 3, NOW)
    claimed = store.claim("worker", NOW, timedelta(minutes=1))
    assert claimed is not None
    # Claim order is deterministic; retain the actual running job and normalize the rest.
    running = claimed.id
    for job_id, status in ((queued, "queued"), (succeeded, "succeeded"), (failed, "failed")):
        if job_id == running:
            continue
        set_job(
            database,
            job_id,
            status=status,
            attempt=1 if status != "queued" else 0,
            last_error="LeaseExpired" if status == "failed" else None,
        )

    items = client.get(f"/projects/{project.id}/jobs").json()["items"]
    statuses = {item["status"] for item in items}
    assert {"queued", "running", "succeeded", "failed"}.issubset(statuses)
    failed_item = next(item for item in items if item["status"] == "failed")
    assert failed_item["error_class"] == "LeaseExpired"
    assert failed_item["recoverable"] is False


def test_job_routes_use_frozen_not_found_and_validation_shapes(
    database: Database, client: TestClient
) -> None:
    project = create_project(database)
    empty = client.get(f"/projects/{project.id}/jobs")
    missing_project = client.get(f"/projects/{uuid4()}/jobs")
    missing_job = client.get(f"/jobs/{uuid4()}")
    invalid_project = client.get("/projects/not-a-uuid/jobs")
    invalid_job = client.get("/jobs/not-a-uuid")

    assert empty.status_code == 200 and empty.json() == {"items": []}
    assert missing_project.status_code == 404
    assert missing_project.json()["detail"]["code"] == "not_found"
    assert missing_job.status_code == 404
    assert missing_job.json() == {"detail": {"code": "not_found", "message": "job not found"}}
    assert invalid_project.status_code == invalid_job.status_code == 422


def test_openapi_job_models_do_not_expose_sensitive_fields(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    properties = schema["components"]["schemas"]["JobResponse"]["properties"]

    assert set(properties) == {
        "id",
        "project_id",
        "kind",
        "status",
        "attempt",
        "max_attempts",
        "available_at",
        "created_at",
        "updated_at",
        "error_class",
        "recoverable",
    }
    assert {"payload", "lease_owner", "lease_expires_at"}.isdisjoint(properties)


def test_only_latest_generation_reservation_is_recoverable(
    database: Database, client: TestClient
) -> None:
    project = create_project(database)
    repository = GenerationRequestRepository(database)
    first = repository.reserve_and_enqueue(
        FactCardGenerationSpec(project.id, (uuid4(),), "v1", 10), "first", NOW, 2
    )
    set_job(database, first.job_id, status="failed", attempt=2, last_error="PermanentError")
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE projects SET status='failed', failed_stage='generation',
            revision=revision+1, updated_at=%s WHERE id=%s""",
            (NOW + timedelta(seconds=1), project.id),
        )
    second = repository.reserve_and_enqueue(
        FactCardGenerationSpec(project.id, (uuid4(),), "v1", 10),
        "second",
        NOW + timedelta(seconds=2),
        2,
    )
    set_job(
        database,
        second.job_id,
        status="succeeded",
        attempt=1,
        updated_at=NOW + timedelta(seconds=3),
    )
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE projects SET status='failed', failed_stage='generation',
            revision=revision+1, updated_at=%s WHERE id=%s""",
            (NOW + timedelta(seconds=3), project.id),
        )

    items = client.get(f"/projects/{project.id}/jobs").json()["items"]
    by_id = {item["id"]: item for item in items}

    assert by_id[str(first.job_id)]["recoverable"] is False
    assert by_id[str(second.job_id)]["recoverable"] is True


def test_expired_lease_reports_persisted_state_then_cleanup_state(
    database: Database, client: TestClient
) -> None:
    project = create_project(database)
    store = JobStore(database)
    job_id = store.enqueue("expiring", project.id, {}, 1, NOW)
    claimed = store.claim("worker", NOW, timedelta(seconds=5))
    assert claimed is not None and claimed.id == job_id

    before_cleanup = client.get(f"/jobs/{job_id}").json()
    assert before_cleanup["status"] == "running"
    assert before_cleanup["error_class"] is None

    assert store.claim("replacement", NOW + timedelta(seconds=5), timedelta(seconds=5)) is None
    after_cleanup = client.get(f"/jobs/{job_id}").json()
    assert after_cleanup["status"] == "failed"
    assert after_cleanup["error_class"] == "LeaseExpired"

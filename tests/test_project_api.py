from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app, get_project_repository, utc_now
from apps.common.database import Database
from packages.domain import (
    ArtifactKind,
    ArtifactRef,
    ConcurrentUpdateError,
    ContentProject,
    FailedStage,
    ImmutableConflictError,
    InvalidStateTransition,
    ProjectRepository,
    ProjectStatus,
    Review,
    ReviewDecision,
    ReviewRepository,
)

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


class InMemoryProjectRepository:
    def __init__(self) -> None:
        self.projects: dict[UUID, ContentProject] = {}
        self.reviews: list[Review] = []

    def create(self, project: ContentProject) -> None:
        self.projects[project.id] = project

    def list(self) -> tuple[ContentProject, ...]:
        return tuple(
            sorted(
                self.projects.values(),
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )
        )

    def get(self, project_id: UUID) -> ContentProject:
        from packages.domain import EntityNotFoundError

        try:
            return self.projects[project_id]
        except KeyError as error:
            raise EntityNotFoundError(f"project not found: {project_id}") from error

    def apply_review(
        self,
        project: ContentProject,
        review: Review,
        expected_revision: int,
    ) -> None:
        current = self.get(project.id)
        if current.revision != expected_revision:
            raise ConcurrentUpdateError("stale revision")
        self.projects[project.id] = project
        self.reviews.append(review)


@pytest.fixture
def api() -> Iterator[tuple[TestClient, InMemoryProjectRepository]]:
    repository = InMemoryProjectRepository()
    app.dependency_overrides[get_project_repository] = lambda: repository
    app.dependency_overrides[utc_now] = lambda: NOW
    try:
        yield TestClient(app), repository
    finally:
        app.dependency_overrides.clear()


def project_at(
    status: ProjectStatus,
    *,
    title: str = "Review me",
    created_at: datetime = NOW,
) -> ContentProject:
    return ContentProject(
        id=uuid4(),
        title=title,
        status=status,
        created_at=created_at,
        updated_at=created_at,
        current_artifacts={
            ArtifactKind.SCRIPT: ArtifactRef(uuid4(), 1, ArtifactKind.SCRIPT, "a" * 64)
        },
    )


def test_create_list_and_get_projects(
    api: tuple[TestClient, InMemoryProjectRepository],
) -> None:
    client, repository = api
    older = project_at(ProjectStatus.DRAFT, title="Older", created_at=NOW - timedelta(days=1))
    repository.create(older)

    created = client.post("/projects", json={"title": "  New project  "})

    assert created.status_code == 201
    created_body = created.json()
    assert created_body["title"] == "New project"
    assert created_body["status"] == "draft"
    assert created_body["revision"] == 1
    assert created_body["failed_stage"] is None
    assert created_body["current_artifacts"] == {}

    listed = client.get("/projects")
    assert listed.status_code == 200
    assert [item["title"] for item in listed.json()["items"]] == ["New project", "Older"]

    detail = client.get(f"/projects/{older.id}")
    assert detail.status_code == 200
    assert detail.json()["current_artifacts"]["script"]["kind"] == "script"


@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [("approve", "approved"), ("reject", "failed")],
)
def test_review_transitions_project_and_records_review(
    api: tuple[TestClient, InMemoryProjectRepository],
    decision: str,
    expected_status: str,
) -> None:
    client, repository = api
    project = project_at(ProjectStatus.REVIEW_REQUIRED)
    repository.create(project)

    response = client.post(
        f"/projects/{project.id}/reviews",
        json={"decision": decision, "note": "  Checked against sources.  ", "expected_revision": 1},
    )

    assert response.status_code == 201
    assert response.json()["note"] == "Checked against sources."
    assert response.json()["project_revision"] == 2
    assert repository.get(project.id).status.value == expected_status
    assert repository.get(project.id).revision == 2
    assert repository.get(project.id).failed_stage is (
        None if decision == "approve" else FailedStage.GENERATION
    )
    assert len(repository.reviews) == 1


def test_not_found_uses_frozen_error_shape(
    api: tuple[TestClient, InMemoryProjectRepository],
) -> None:
    client, _repository = api

    response = client.get(f"/projects/{uuid4()}")

    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "not_found", "message": "project not found"}}


def test_stale_revision_does_not_write_review(
    api: tuple[TestClient, InMemoryProjectRepository],
) -> None:
    client, repository = api
    project = replace(project_at(ProjectStatus.REVIEW_REQUIRED), revision=2)
    repository.create(project)

    response = client.post(
        f"/projects/{project.id}/reviews",
        json={"decision": "approve", "note": "Looks good", "expected_revision": 1},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "revision_conflict"
    assert repository.get(project.id) == project
    assert repository.reviews == []


def test_invalid_transition_does_not_write_review(
    api: tuple[TestClient, InMemoryProjectRepository],
) -> None:
    client, repository = api
    project = project_at(ProjectStatus.DRAFT)
    repository.create(project)

    response = client.post(
        f"/projects/{project.id}/reviews",
        json={"decision": "approve", "note": "Looks good", "expected_revision": 1},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "invalid_transition"
    assert repository.get(project.id) == project
    assert repository.reviews == []


@pytest.mark.parametrize("note", ["", "   ", "x" * 2001])
def test_invalid_note_keeps_fastapi_validation_and_does_not_write_review(
    api: tuple[TestClient, InMemoryProjectRepository],
    note: str,
) -> None:
    client, repository = api
    project = project_at(ProjectStatus.REVIEW_REQUIRED)
    repository.create(project)

    response = client.post(
        f"/projects/{project.id}/reviews",
        json={"decision": "approve", "note": note, "expected_revision": 1},
    )

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert repository.get(project.id) == project
    assert repository.reviews == []


@pytest.mark.parametrize(
    ("decision", "target", "failed_stage"),
    [
        (ReviewDecision.APPROVE, ProjectStatus.APPROVED, None),
        (ReviewDecision.REJECT, ProjectStatus.FAILED, FailedStage.GENERATION),
    ],
)
def test_repository_review_is_atomic_when_review_insert_fails(
    database: Database,
    decision: ReviewDecision,
    target: ProjectStatus,
    failed_stage: FailedStage | None,
) -> None:
    projects = ProjectRepository(database)
    reviews = ReviewRepository(database)
    project = replace(project_at(ProjectStatus.REVIEW_REQUIRED), current_artifacts={})
    projects.create(project)
    updated = replace(
        project,
        status=target,
        failed_stage=failed_stage,
        revision=2,
        updated_at=NOW,
    )
    review = Review(uuid4(), project.id, decision, "Reviewed", 2, NOW)
    reviews.add(review)

    with pytest.raises(ImmutableConflictError):
        projects.apply_review(updated, review, expected_revision=1)

    assert projects.get(project.id) == project


@pytest.mark.parametrize(
    ("decision", "target"),
    [
        (ReviewDecision.APPROVE, ProjectStatus.APPROVED),
        (ReviewDecision.REJECT, ProjectStatus.FAILED),
    ],
)
def test_repository_review_success_on_postgresql(
    database: Database,
    decision: ReviewDecision,
    target: ProjectStatus,
) -> None:
    projects = ProjectRepository(database)
    project = replace(project_at(ProjectStatus.REVIEW_REQUIRED), current_artifacts={})
    projects.create(project)
    updated = replace(
        project,
        title="attempted title mutation",
        status=target,
        revision=2,
        updated_at=NOW,
        failed_stage=FailedStage.GENERATION if decision is ReviewDecision.REJECT else None,
    )
    review = Review(uuid4(), project.id, decision, "Reviewed", 2, NOW)

    projects.apply_review(updated, review, expected_revision=1)

    stored = projects.get(project.id)
    assert stored.status is target
    assert stored.revision == 2
    assert stored.title == project.title
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT decision, project_revision FROM reviews WHERE id = %s", (review.id,))
        assert cursor.fetchone() == (decision.value, 2)


@pytest.mark.parametrize(
    ("decision", "status", "failed_stage"),
    [
        (ReviewDecision.APPROVE, ProjectStatus.GENERATING, None),
        (ReviewDecision.APPROVE, ProjectStatus.FAILED, FailedStage.GENERATION),
        (ReviewDecision.REJECT, ProjectStatus.APPROVED, None),
        (ReviewDecision.REJECT, ProjectStatus.FAILED, FailedStage.PUBLICATION),
    ],
)
def test_repository_rejects_review_decision_status_mismatch(
    database: Database,
    decision: ReviewDecision,
    status: ProjectStatus,
    failed_stage: FailedStage | None,
) -> None:
    projects = ProjectRepository(database)
    project = replace(project_at(ProjectStatus.REVIEW_REQUIRED), current_artifacts={})
    projects.create(project)
    mismatched = replace(
        project,
        status=status,
        failed_stage=failed_stage,
        revision=2,
    )
    review = Review(uuid4(), project.id, decision, "Reviewed", 2, NOW)

    with pytest.raises(InvalidStateTransition, match="cannot produce"):
        projects.apply_review(mismatched, review, expected_revision=1)

    assert projects.get(project.id) == project


def test_concurrent_reviews_only_commit_one_record(database: Database) -> None:
    projects = ProjectRepository(database)
    project = replace(project_at(ProjectStatus.REVIEW_REQUIRED), current_artifacts={})
    projects.create(project)
    barrier = Barrier(2)

    def apply(decision: ReviewDecision) -> str:
        target = (
            ProjectStatus.APPROVED if decision is ReviewDecision.APPROVE else ProjectStatus.FAILED
        )
        updated = replace(
            project,
            status=target,
            revision=2,
            failed_stage=(FailedStage.GENERATION if decision is ReviewDecision.REJECT else None),
        )
        review = Review(uuid4(), project.id, decision, "Concurrent review", 2, NOW)
        barrier.wait()
        try:
            ProjectRepository(database).apply_review(updated, review, expected_revision=1)
        except ConcurrentUpdateError:
            return "conflict"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(apply, (ReviewDecision.APPROVE, ReviewDecision.REJECT)))

    assert sorted(results) == ["committed", "conflict"]
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM reviews WHERE project_id = %s", (project.id,))
        assert cursor.fetchone() == (1,)

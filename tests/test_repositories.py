from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest

from apps.common.database import Database
from migrations import run_migrations
from packages.domain import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    ArtifactRepository,
    ConcurrentUpdateError,
    ContentProject,
    ImmutableConflictError,
    ProjectRepository,
    ProjectStatus,
    Source,
    SourceCitation,
    SourceExcerpt,
    SourceKind,
    SourceRepository,
)

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64


def make_project() -> ContentProject:
    return ContentProject(uuid4(), "Persistence demo", ProjectStatus.DRAFT, NOW, NOW)


def make_source() -> Source:
    return Source(
        id=uuid4(),
        kind=SourceKind.OFFICIAL_DOCUMENTATION,
        title="Official guide",
        uri="https://example.test/guide",
        retrieved_at=NOW,
        sha256=SHA_A,
        summary="Stable source summary.",
        excerpts=(
            SourceExcerpt(uuid4(), "Second excerpt in UUID-independent order.", "section-2"),
            SourceExcerpt(uuid4(), "First-class citable evidence.", "section-1"),
        ),
    )


def test_migration_is_ordered_and_idempotent(
    migrated_database: tuple[Database, tuple[str, ...]],
) -> None:
    database, initially_applied = migrated_database
    assert initially_applied == ("001_initial.sql", "002_generation_requests.sql")
    assert run_migrations(database) == ()


def test_generation_request_migration_preserves_existing_001_data(
    legacy_database: Database,
) -> None:
    project_id = uuid4()
    job_id = uuid4()
    artifact_id = uuid4()
    with legacy_database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO projects
                (id, title, status, created_at, updated_at, revision)
            VALUES (%s, 'Legacy project', 'draft', %s, %s, 1)
            """,
            (project_id, NOW, NOW),
        )
        cursor.execute(
            """
            INSERT INTO jobs
                (id, project_id, kind, status, max_attempts, available_at,
                 created_at, updated_at)
            VALUES (%s, %s, 'legacy', 'queued', 3, %s, %s, %s)
            """,
            (job_id, project_id, NOW, NOW, NOW),
        )
        cursor.execute(
            """
            INSERT INTO artifacts
                (id, version, kind, sha256, project_id, storage_path, created_at,
                 created_by, adapter)
            VALUES (%s, 1, 'fact_card', %s, %s, 'legacy/fact-card.json', %s,
                    'legacy', 'legacy@1')
            """,
            (artifact_id, SHA_A, project_id, NOW),
        )

    assert run_migrations(legacy_database) == ("002_generation_requests.sql",)
    with legacy_database.connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT title FROM projects WHERE id = %s", (project_id,))
        assert cursor.fetchone() == ("Legacy project",)
        cursor.execute("SELECT kind FROM jobs WHERE id = %s", (job_id,))
        assert cursor.fetchone() == ("legacy",)
        cursor.execute("SELECT version FROM artifacts WHERE id = %s", (artifact_id,))
        assert cursor.fetchone() == (1,)
        cursor.execute("SELECT COUNT(*) FROM generation_requests")
        assert cursor.fetchone() == (0,)


def test_project_round_trip_and_optimistic_concurrency(database: Database) -> None:
    repository = ProjectRepository(database)
    original = make_project()
    repository.create(original)
    assert repository.get(original.id) == original

    barrier = Barrier(2)

    def update_as(title: str) -> tuple[str, str]:
        candidate = replace(
            original,
            title=title,
            revision=2,
            updated_at=datetime(2026, 8, 9, 13, tzinfo=UTC),
        )
        barrier.wait()
        try:
            repository.update(candidate, expected_revision=1)
        except ConcurrentUpdateError:
            return ("conflict", title)
        return ("updated", title)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(update_as, ("First writer", "Second writer")))

    assert sorted(result[0] for result in results) == ["conflict", "updated"]
    winning_title = next(title for status, title in results if status == "updated")
    assert repository.get(original.id).title == winning_title
    assert repository.get(original.id).revision == 2


def test_source_artifact_lineage_and_current_pointer_round_trip(database: Database) -> None:
    projects = ProjectRepository(database)
    sources = SourceRepository(database)
    artifacts = ArtifactRepository(database)
    project = make_project()
    source = make_source()
    projects.create(project)
    sources.add(source)
    assert sources.get(source.id) == source

    upstream = Artifact(
        ref=ArtifactRef(uuid4(), 1, ArtifactKind.FACT_CARD, SHA_A),
        project_id=project.id,
        storage_path="projects/demo/fact-card-v1.json",
        created_at=NOW,
        created_by="author",
        adapter="manual@1",
        citations=(SourceCitation(source.id, source.excerpts[0].id, source.sha256),),
        metadata={"facts": ({"confidence": 1.0},)},
    )
    generated = Artifact(
        ref=ArtifactRef(uuid4(), 1, ArtifactKind.SCRIPT, SHA_B),
        project_id=project.id,
        storage_path="projects/demo/script-v1.json",
        created_at=NOW,
        created_by="fake-provider",
        adapter="fake@1",
        upstream=(upstream.ref,),
        citations=(SourceCitation(source.id, source.excerpts[1].id, source.sha256),),
        metadata={"settings": {"temperature": 0}, "labels": ("draft",)},
    )
    artifacts.add(upstream)
    artifacts.add(generated)

    current = replace(
        project,
        revision=2,
        current_artifacts={ArtifactKind.SCRIPT: generated.ref},
    )
    projects.update(current, expected_revision=1)
    assert projects.get(project.id) == current
    assert artifacts.get(generated.ref.artifact_id, 1) == generated
    listed = artifacts.list_for_project(project.id)
    assert {item.ref: item for item in listed} == {
        upstream.ref: upstream,
        generated.ref: generated,
    }


def test_immutable_entities_reject_duplicate_keys(database: Database) -> None:
    projects = ProjectRepository(database)
    sources = SourceRepository(database)
    artifacts = ArtifactRepository(database)
    project = make_project()
    source = make_source()
    projects.create(project)
    sources.add(source)
    with pytest.raises(ImmutableConflictError):
        sources.add(source)

    artifact = Artifact(
        ArtifactRef(uuid4(), 1, ArtifactKind.SCRIPT, SHA_A),
        project.id,
        "projects/demo/script.json",
        NOW,
        "author",
        "manual@1",
    )
    artifacts.add(artifact)
    with pytest.raises(ImmutableConflictError):
        artifacts.add(artifact)


def test_database_rejects_corruption_and_immutable_history_changes(database: Database) -> None:
    source = make_source()
    SourceRepository(database).add(source)
    with database.connect() as connection, connection.cursor() as cursor:
        with pytest.raises(psycopg.IntegrityError):
            cursor.execute("UPDATE sources SET sha256 = 'broken' WHERE id = %s", (source.id,))
    with database.connect() as connection, connection.cursor() as cursor:
        with pytest.raises(psycopg.IntegrityError):
            cursor.execute("DELETE FROM sources WHERE id = %s", (source.id,))


def test_current_pointer_requires_existing_project_artifact(database: Database) -> None:
    repository = ProjectRepository(database)
    project = make_project()
    repository.create(project)
    missing = ArtifactRef(uuid4(), 1, ArtifactKind.SCRIPT, SHA_A)
    invalid = replace(
        project,
        revision=2,
        current_artifacts={ArtifactKind.SCRIPT: missing},
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        repository.update(invalid, expected_revision=1)
    assert repository.get(project.id) == project

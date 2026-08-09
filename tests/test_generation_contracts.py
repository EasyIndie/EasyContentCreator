from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from apps.common.database import Database
from packages.domain import (
    ContentProject,
    InvalidStateTransition,
    ProjectRepository,
    ProjectStatus,
    Source,
    SourceExcerpt,
    SourceKind,
)
from packages.pipeline import (
    EvidenceSourceError,
    FactCardGenerationSpec,
    GenerationRequestConflict,
    GenerationRequestRepository,
    canonical_request_hash,
    citations_for_sources,
    fact_card_artifact_id,
)

NOW = datetime.now(UTC) + timedelta(seconds=1)


def create_project(database: Database) -> ContentProject:
    project = ContentProject(uuid4(), "Generation", ProjectStatus.DRAFT, NOW, NOW)
    ProjectRepository(database).create(project)
    return project


def spec_for(
    project_id: UUID, *source_ids: UUID, budget_units: int = 100
) -> FactCardGenerationSpec:
    return FactCardGenerationSpec(project_id, tuple(source_ids), "fact-card-v1", budget_units)


def row_count(database: Database, table: str) -> int:
    assert table in {"generation_requests", "jobs"}
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        return cursor.fetchone()[0]


def mark_generation_failed(database: Database, project_id: UUID, occurred_at: datetime) -> None:
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE projects
            SET status = 'failed', failed_stage = 'generation', revision = revision + 1,
                updated_at = %s
            WHERE id = %s AND status = 'generating'
            """,
            (occurred_at, project_id),
        )
        assert cursor.rowcount == 1
        cursor.execute(
            """
            UPDATE jobs SET status = 'failed', updated_at = %s
            WHERE project_id = %s AND status = 'queued'
            """,
            (occurred_at, project_id),
        )


def test_spec_validation_and_canonical_hash_are_stable() -> None:
    project_id = uuid4()
    first_source, second_source = uuid4(), uuid4()
    first = spec_for(project_id, second_source, first_source)
    reordered = spec_for(project_id, first_source, second_source)

    assert first.source_ids == tuple(sorted((first_source, second_source), key=str))
    assert canonical_request_hash(first) == canonical_request_hash(reordered)
    assert canonical_request_hash(first) != canonical_request_hash(
        spec_for(project_id, first_source, second_source, budget_units=101)
    )
    with pytest.raises(ValueError, match="empty"):
        FactCardGenerationSpec(project_id, (), "v1", 1)
    with pytest.raises(ValueError, match="duplicates"):
        FactCardGenerationSpec(project_id, (first_source, first_source), "v1", 1)
    with pytest.raises(ValueError, match="template"):
        FactCardGenerationSpec(project_id, (first_source,), " ", 1)
    with pytest.raises(ValueError, match="budget"):
        FactCardGenerationSpec(project_id, (first_source,), "v1", 0)


def test_citations_cover_all_excerpts_in_stable_order() -> None:
    source_a = Source(
        UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        SourceKind.PAPER,
        "A",
        "https://example.test/a",
        NOW,
        "a" * 64,
        "Summary A",
        (
            SourceExcerpt(UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"), "Later"),
            SourceExcerpt(UUID("11111111-1111-1111-1111-111111111111"), "Earlier"),
        ),
    )
    source_b = Source(
        UUID("22222222-2222-2222-2222-222222222222"),
        SourceKind.INSTITUTION,
        "B",
        "https://example.test/b",
        NOW,
        "b" * 64,
        "Summary B",
        (SourceExcerpt(UUID("33333333-3333-3333-3333-333333333333"), "Evidence"),),
    )

    citations = citations_for_sources((source_a, source_b))

    assert len(citations) == 3
    assert [(str(item.source_id), str(item.excerpt_id)) for item in citations] == sorted(
        (str(item.source_id), str(item.excerpt_id)) for item in citations
    )
    assert {item.source_sha256 for item in citations} == {"a" * 64, "b" * 64}

    empty = replace(source_a, id=uuid4(), excerpts=())
    with pytest.raises(EvidenceSourceError, match="excerpt"):
        citations_for_sources((empty,))


def test_same_key_same_hash_reuses_one_reservation_and_job(database: Database) -> None:
    project = create_project(database)
    spec = spec_for(project.id, uuid4())
    repository = GenerationRequestRepository(database)

    first = repository.reserve_and_enqueue(spec, "request-1", NOW, 3)
    second = repository.reserve_and_enqueue(spec, "request-1", NOW, 3)

    assert second == first
    assert first.artifact_id == fact_card_artifact_id(project.id)
    assert first.artifact_version == 1
    assert row_count(database, "generation_requests") == 1
    assert row_count(database, "jobs") == 1


def test_same_key_concurrent_requests_share_reservation(database: Database) -> None:
    project = create_project(database)
    spec = spec_for(project.id, uuid4())
    barrier = Barrier(2)

    def reserve() -> object:
        barrier.wait()
        return GenerationRequestRepository(database).reserve_and_enqueue(
            spec, "concurrent-key", NOW, 3
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        reservations = tuple(executor.map(lambda _index: reserve(), range(2)))

    assert reservations[0] == reservations[1]
    assert row_count(database, "generation_requests") == 1
    assert row_count(database, "jobs") == 1


def test_same_key_different_hash_conflicts_without_writes(database: Database) -> None:
    project = create_project(database)
    source_id = uuid4()
    repository = GenerationRequestRepository(database)
    original = repository.reserve_and_enqueue(spec_for(project.id, source_id), "same-key", NOW, 3)
    project_after_reservation = ProjectRepository(database).get(project.id)

    with pytest.raises(GenerationRequestConflict):
        repository.reserve_and_enqueue(
            spec_for(project.id, source_id, budget_units=999), "same-key", NOW, 3
        )

    assert row_count(database, "generation_requests") == 1
    assert row_count(database, "jobs") == 1
    assert ProjectRepository(database).get(project.id) == project_after_reservation
    assert (
        repository.reserve_and_enqueue(spec_for(project.id, source_id), "same-key", NOW, 3)
        == original
    )


def test_different_keys_concurrently_keep_single_active_generation(database: Database) -> None:
    project = create_project(database)
    source_id = uuid4()
    barrier = Barrier(2)

    def reserve(key: str) -> tuple[str, object]:
        barrier.wait()
        try:
            value = GenerationRequestRepository(database).reserve_and_enqueue(
                spec_for(project.id, source_id), key, NOW, 3
            )
        except InvalidStateTransition as error:
            return ("rejected", error)
        return ("reserved", value)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(reserve, ("key-a", "key-b")))

    assert sorted(result[0] for result in results) == ["rejected", "reserved"]
    assert row_count(database, "generation_requests") == 1
    assert row_count(database, "jobs") == 1
    reservation = next(value for status, value in results if status == "reserved")
    assert reservation.artifact_version == 1


def test_generation_failure_recovery_reserves_next_version(database: Database) -> None:
    project = create_project(database)
    source_id = uuid4()
    repository = GenerationRequestRepository(database)
    first = repository.reserve_and_enqueue(spec_for(project.id, source_id), "attempt-1", NOW, 3)
    failed_at = NOW + timedelta(seconds=1)
    mark_generation_failed(database, project.id, failed_at)

    second = repository.reserve_and_enqueue(
        spec_for(project.id, source_id), "attempt-2", failed_at + timedelta(seconds=1), 3
    )

    assert second.artifact_id == first.artifact_id
    assert second.artifact_version == first.artifact_version + 1 == 2
    assert second.job_id != first.job_id
    assert row_count(database, "generation_requests") == 2
    stored = ProjectRepository(database).get(project.id)
    assert stored.status is ProjectStatus.GENERATING
    assert stored.failed_stage is None

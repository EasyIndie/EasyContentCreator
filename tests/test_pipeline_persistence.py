from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from uuid import UUID, uuid4

import psycopg
import pytest

from apps.common.database import Database
from migrations import run_migrations
from packages.domain import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    ArtifactRepository,
    BoundArtifactRef,
    ContentProject,
    LogicalArtifactRef,
    PipelineRun,
    PipelineRunStatus,
    ProjectRepository,
    ProjectStatus,
    SourceRepository,
    StepKind,
    StepOutputSpec,
    StepRunSpec,
)
from packages.pipeline import (
    GenerationRequestRepository,
    PipelineDefinitionRepository,
    PipelineRunRepository,
    StepRunConflict,
    StepRunRepository,
    definition_digest,
    definition_manifest,
    short_video_v1_definition,
)

NOW = datetime.now(UTC) + timedelta(seconds=1)
MIGRATIONS_DIR = Path(__file__).parents[1] / "migrations"


def create_artifact(
    database: Database,
    project_id: UUID,
    kind: ArtifactKind = ArtifactKind.FACT_CARD,
    *,
    artifact_id: UUID | None = None,
    version: int = 1,
    sha: str = "a" * 64,
) -> Artifact:
    artifact = Artifact(
        ArtifactRef(artifact_id or uuid4(), version, kind, sha),
        project_id,
        f"projects/{project_id}/{kind.value}-{version}.json",
        NOW,
        "fixture",
        "fixture@1",
    )
    ArtifactRepository(database).add(artifact)
    return artifact


def setup_run(database: Database) -> tuple[ContentProject, Artifact, PipelineRun]:
    project = ContentProject(uuid4(), "Pipeline", ProjectStatus.DRAFT, NOW, NOW)
    ProjectRepository(database).create(project)
    fact = create_artifact(database, project.id)
    definition = short_video_v1_definition()
    digest = PipelineDefinitionRepository(database).register(definition, NOW)
    run = PipelineRun(
        uuid4(),
        project.id,
        definition.kind,
        definition.version,
        definition.profile_version,
        (LogicalArtifactRef(fact.ref, "fact_card"),),
        PipelineRunStatus.PENDING,
        NOW,
    )
    PipelineRunRepository(database).create(run, digest)
    return project, fact, run


def topic_spec(project: ContentProject, fact: Artifact, run: PipelineRun) -> StepRunSpec:
    return StepRunSpec(
        project.id,
        run.id,
        StepKind.TOPIC_BRIEF,
        "v1",
        (BoundArtifactRef("fact_card", LogicalArtifactRef(fact.ref, "fact_card")),),
        (StepOutputSpec("topic_brief", ArtifactKind.TOPIC_BRIEF, "topic_brief"),),
        {"budget": 3},
        "short_video_9x16_v1",
    )


def count(database: Database, table: str) -> int:
    allowed = {
        "jobs",
        "logical_artifact_streams",
        "pipeline_runs",
        "step_output_reservations",
        "step_runs",
    }
    assert table in allowed
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        return cursor.fetchone()[0]


def test_definition_pipeline_and_step_round_trip(database: Database) -> None:
    project, fact, run = setup_run(database)
    definition = short_video_v1_definition()
    digest = definition_digest(definition)
    record = PipelineDefinitionRepository(database).get(digest)
    assert (record.pipeline_kind, record.pipeline_version, record.profile_version) == (
        definition.kind,
        definition.version,
        definition.profile_version,
    )
    expected_manifest = json.loads(json.dumps(definition_manifest(definition)))
    assert record.manifest == expected_manifest
    assert PipelineRunRepository(database).get(run.id) == run

    reserved = StepRunRepository(database).reserve_and_enqueue(
        topic_spec(project, fact, run), "topic-1", NOW, 3
    )
    assert StepRunRepository(database).get(reserved.id) == reserved
    assert reserved.output_reservations[0].version == 1
    assert count(database, "jobs") == 1


def test_same_key_concurrently_reuses_one_step_and_job(database: Database) -> None:
    project, fact, run = setup_run(database)
    request = topic_spec(project, fact, run)
    barrier = Barrier(2)

    def reserve() -> object:
        barrier.wait()
        return StepRunRepository(database).reserve_and_enqueue(request, "same-key", NOW, 3)

    with ThreadPoolExecutor(max_workers=2) as executor:
        values = tuple(executor.map(lambda _index: reserve(), range(2)))
    assert values[0] == values[1]
    assert count(database, "step_runs") == count(database, "jobs") == 1


def test_same_key_different_hash_and_active_fingerprint_conflict(database: Database) -> None:
    project, fact, run = setup_run(database)
    request = topic_spec(project, fact, run)
    repository = StepRunRepository(database)
    repository.reserve_and_enqueue(request, "first", NOW, 3)
    with pytest.raises(StepRunConflict, match="different request"):
        repository.reserve_and_enqueue(replace(request, parameters={"budget": 4}), "first", NOW, 3)
    with pytest.raises(StepRunConflict, match="fingerprint"):
        repository.reserve_and_enqueue(request, "second", NOW, 3)
    assert count(database, "step_runs") == count(database, "jobs") == 1


def test_fanout_streams_coexist_and_versions_are_monotonic(database: Database) -> None:
    project, _fact, run = setup_run(database)
    storyboard = create_artifact(database, project.id, ArtifactKind.STORYBOARD)
    outputs = (
        StepOutputSpec("asset_manifest", ArtifactKind.ASSET_MANIFEST, "asset_manifest"),
        StepOutputSpec("shot_asset", ArtifactKind.MEDIA, "asset/shot_001", "shot_001"),
        StepOutputSpec("shot_asset", ArtifactKind.MEDIA, "asset/shot_002", "shot_002"),
    )
    request = StepRunSpec(
        project.id,
        run.id,
        StepKind.ASSET_MANIFEST,
        "v1",
        (BoundArtifactRef("storyboard", LogicalArtifactRef(storyboard.ref, "storyboard")),),
        outputs,
        {},
        "short_video_9x16_v1",
    )
    repository = StepRunRepository(database)
    first = repository.reserve_and_enqueue(request, "assets-1", NOW, 3)
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE step_runs SET status = 'failed', finished_at = %s,
                error_class = 'Provider.PermanentError' WHERE id = %s
            """,
            (NOW + timedelta(seconds=1), first.id),
        )
        cursor.execute(
            "UPDATE jobs SET status = 'failed', updated_at = %s WHERE id = %s",
            (NOW + timedelta(seconds=1), first.job_id),
        )
    with pytest.raises(StepRunConflict, match="explicit rerun"):
        repository.reserve_and_enqueue(
            request, "assets-without-explicit-rerun", NOW + timedelta(seconds=2), 3
        )
    recovery_spec = replace(request, rerun_of_step_run_id=first.id)
    second = repository.reserve_and_enqueue(
        recovery_spec, "assets-2", NOW + timedelta(seconds=2), 3
    )
    assert [item.artifact_id for item in second.output_reservations] == [
        item.artifact_id for item in first.output_reservations
    ]
    assert {item.version for item in second.output_reservations} == {2}
    assert len({item.output.logical_key for item in second.output_reservations}) == 3


def test_reservation_failure_rolls_back_stream_job_and_step(database: Database) -> None:
    project, fact, run = setup_run(database)
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE FUNCTION fail_step_reservation() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected reservation failure'; END; $$
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER fail_step_reservation_insert
            BEFORE INSERT ON step_output_reservations
            FOR EACH ROW EXECUTE FUNCTION fail_step_reservation()
            """
        )
    try:
        with pytest.raises(psycopg.DatabaseError, match="injected reservation failure"):
            StepRunRepository(database).reserve_and_enqueue(
                topic_spec(project, fact, run), "rollback", NOW, 3
            )
    finally:
        with database.connect() as connection, connection.cursor() as cursor:
            cursor.execute("DROP TRIGGER fail_step_reservation_insert ON step_output_reservations")
            cursor.execute("DROP FUNCTION fail_step_reservation()")
    assert count(database, "step_runs") == count(database, "jobs") == 0
    assert count(database, "logical_artifact_streams") == 0


def test_upgrade_from_002_with_existing_data_preserves_rows_and_starts_closed(
    legacy_database: Database,
) -> None:
    project_id = uuid4()
    artifact_id = uuid4()
    source_id = uuid4()
    excerpt_id = uuid4()
    job_id = uuid4()
    with legacy_database.connect() as connection, connection.cursor() as cursor:
        cursor.execute((MIGRATIONS_DIR / "002_generation_requests.sql").read_text(encoding="utf-8"))
        cursor.execute(
            "INSERT INTO schema_migrations (name) VALUES ('002_generation_requests.sql')"
        )
        cursor.execute(
            """
            INSERT INTO projects VALUES (%s, 'Legacy', 'approved', %s, %s, 2, NULL)
            """,
            (project_id, NOW, NOW),
        )
        cursor.execute(
            """
            INSERT INTO sources VALUES
                (%s, 'official_documentation', 'Legacy source', 'https://example.test/legacy',
                 %s, %s, 'Legacy summary')
            """,
            (source_id, NOW, "c" * 64),
        )
        cursor.execute(
            """
            INSERT INTO source_excerpts VALUES (%s, %s, 0, 'Legacy excerpt', 'section-1')
            """,
            (source_id, excerpt_id),
        )
        cursor.execute(
            """
            INSERT INTO artifacts
                (id, version, kind, sha256, project_id, storage_path, created_at,
                 created_by, adapter)
            VALUES (%s, 1, 'fact_card', %s, %s, 'legacy/fact.json', %s, 'legacy', 'legacy@1')
            """,
            (artifact_id, "a" * 64, project_id, NOW),
        )
        cursor.execute(
            "INSERT INTO project_current_artifacts VALUES (%s, 'fact_card', %s, 1)",
            (project_id, artifact_id),
        )
        cursor.execute(
            """
            INSERT INTO artifact_citations
                (artifact_id, artifact_version, position, source_id, excerpt_id, source_sha256)
            VALUES (%s, 1, 0, %s, %s, %s)
            """,
            (artifact_id, source_id, excerpt_id, "c" * 64),
        )
        cursor.execute(
            """
            INSERT INTO jobs
                (id, project_id, kind, payload, status, attempt, max_attempts, available_at,
                 created_at, updated_at)
            VALUES (%s, %s, 'fact_card_generation', '{}'::jsonb, 'succeeded', 1, 3,
                    %s, %s, %s)
            """,
            (job_id, project_id, NOW, NOW, NOW),
        )
        cursor.execute(
            """
            INSERT INTO generation_requests
                (project_id, idempotency_key, request_hash, job_id, artifact_id,
                 artifact_version, source_ids, template_version, budget_units,
                 project_revision, created_at)
            VALUES (%s, 'legacy-request', %s, %s, %s, 1, %s, 'v1', 3, 1, %s)
            """,
            (project_id, "d" * 64, job_id, artifact_id, [source_id], NOW),
        )
        cursor.execute(
            """
            INSERT INTO reviews
                (id, project_id, decision, note, project_revision, created_at)
            VALUES (%s, %s, 'approve', 'Legacy approval', 2, %s)
            """,
            (uuid4(), project_id, NOW),
        )
    assert run_migrations(legacy_database, MIGRATIONS_DIR) == ("003_pipeline_runs.sql",)
    project = ProjectRepository(legacy_database).get(project_id)
    artifact = ArtifactRepository(legacy_database).get(artifact_id, 1)
    source = SourceRepository(legacy_database).get(source_id)
    reservation = GenerationRequestRepository(legacy_database).get_by_job_id(job_id)
    assert project.title == "Legacy"
    assert project.current_artifacts[ArtifactKind.FACT_CARD] == artifact.ref
    assert artifact.citations[0].source_id == source.id
    assert reservation.artifact_id == artifact_id
    with legacy_database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT decision, project_revision FROM reviews WHERE project_id = %s", (project_id,)
        )
        assert cursor.fetchone() == ("approve", 2)
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="not activated"):
            cursor.execute(
                """
                INSERT INTO artifacts
                    (id, version, kind, sha256, project_id, storage_path, created_at,
                     created_by, adapter)
                VALUES (%s, 1, 'topic_brief', %s, %s, 'm2/topic.json', %s, 'fixture', 'fake@1')
                """,
                (uuid4(), "b" * 64, project_id, NOW),
            )


def test_m2_activation_is_explicit_and_one_way(legacy_database: Database) -> None:
    run_migrations(legacy_database, MIGRATIONS_DIR)
    with legacy_database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE artifact_kind_activation
            SET m2_enabled = TRUE, enabled_at = %s
            WHERE singleton AND NOT m2_enabled
            """,
            (NOW,),
        )
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="one-way"):
            cursor.execute(
                "UPDATE artifact_kind_activation SET m2_enabled = FALSE, enabled_at = NULL"
            )


def test_database_rejects_duplicate_logical_version(database: Database) -> None:
    project, fact, run = setup_run(database)
    reserved = StepRunRepository(database).reserve_and_enqueue(
        topic_spec(project, fact, run), "first", NOW, 3
    )
    with database.connect() as connection, connection.cursor() as cursor:
        with pytest.raises(psycopg.errors.UniqueViolation):
            cursor.execute(
                """
                INSERT INTO step_output_reservations
                    (step_run_id, project_id, position, slot_name, item_key, logical_key,
                     kind, artifact_id, artifact_version)
                VALUES (%s, %s, 1, 'duplicate', NULL, %s, %s, %s, %s)
                """,
                (
                    reserved.id,
                    project.id,
                    reserved.output_reservations[0].output.logical_key,
                    reserved.output_reservations[0].output.kind.value,
                    reserved.output_reservations[0].artifact_id,
                    reserved.output_reservations[0].version,
                ),
            )


def test_database_rejects_duplicate_single_output_with_null_item_key(database: Database) -> None:
    project, fact, run = setup_run(database)
    reserved = StepRunRepository(database).reserve_and_enqueue(
        topic_spec(project, fact, run), "first", NOW, 3
    )
    output = reserved.output_reservations[0]
    with database.connect() as connection, connection.cursor() as cursor:
        with pytest.raises(psycopg.errors.UniqueViolation):
            cursor.execute(
                """
                INSERT INTO step_output_reservations
                    (step_run_id, project_id, position, slot_name, item_key, logical_key,
                     kind, artifact_id, artifact_version)
                VALUES (%s, %s, 1, %s, NULL, %s, %s, %s, 2)
                """,
                (
                    reserved.id,
                    project.id,
                    output.output.slot_name,
                    output.output.logical_key,
                    output.output.kind.value,
                    output.artifact_id,
                ),
            )


def test_database_rejects_sha_mismatch_and_immutable_bindings(database: Database) -> None:
    project, fact, run = setup_run(database)
    reserved = StepRunRepository(database).reserve_and_enqueue(
        topic_spec(project, fact, run), "first", NOW, 3
    )
    with database.connect() as connection, connection.cursor() as cursor:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cursor.execute(
                """
                INSERT INTO step_run_inputs
                    (step_run_id, project_id, position, binding_name, logical_key,
                     artifact_id, artifact_version, artifact_kind, artifact_sha256)
                VALUES (%s, %s, 1, 'wrong_sha', 'wrong_sha', %s, 1, 'fact_card', %s)
                """,
                (reserved.id, project.id, fact.ref.artifact_id, "f" * 64),
            )
    with database.connect() as connection, connection.cursor() as cursor:
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="immutable"):
            cursor.execute("DELETE FROM step_run_inputs WHERE step_run_id = %s", (reserved.id,))


def test_database_pins_pipeline_job_kind_and_step_identity(database: Database) -> None:
    project, fact, run = setup_run(database)
    reserved = StepRunRepository(database).reserve_and_enqueue(
        topic_spec(project, fact, run), "first", NOW, 3
    )
    with database.connect() as connection, connection.cursor() as cursor:
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="immutable"):
            cursor.execute(
                "UPDATE jobs SET payload = '{}'::jsonb WHERE id = %s", (reserved.job_id,)
            )


def test_database_rejects_steps_for_terminal_run(database: Database) -> None:
    project, fact, run = setup_run(database)
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE pipeline_runs
            SET status = 'failed', started_at = %s, finished_at = %s,
                error_class = 'Pipeline.PermanentError'
            WHERE id = %s
            """,
            (NOW, NOW, run.id),
        )
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="terminal"):
        StepRunRepository(database).reserve_and_enqueue(
            topic_spec(project, fact, run), "too-late", NOW, 3
        )


def test_terminal_transition_and_raw_step_insert_are_serialized(database: Database) -> None:
    project, _fact, run = setup_run(database)
    terminal_locked = Event()
    allow_terminal_commit = Event()
    insert_started = Event()
    step_id = uuid4()
    job_id = uuid4()

    def finish_run() -> None:
        with database.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pipeline_runs
                SET status = 'failed', started_at = %s, finished_at = %s,
                    error_class = 'Pipeline.PermanentError'
                WHERE id = %s
                """,
                (NOW, NOW, run.id),
            )
            terminal_locked.set()
            assert allow_terminal_commit.wait(timeout=5)

    def insert_raw_step() -> None:
        assert terminal_locked.wait(timeout=5)
        insert_started.set()
        with database.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO jobs
                    (id, project_id, kind, payload, status, max_attempts, available_at,
                     created_at, updated_at)
                    VALUES (%s, %s, 'pipeline_step',
                            jsonb_build_object('step_run_id', %s::text),
                        'queued', 3, %s, %s, %s)
                """,
                (job_id, project.id, str(step_id), NOW, NOW, NOW),
            )
            cursor.execute(
                """
                INSERT INTO step_runs
                    (id, project_id, pipeline_run_id, definition_digest, step_kind,
                     step_version, profile_version, parameters, idempotency_key,
                     request_hash, job_id, status, created_at)
                VALUES (%s, %s, %s, %s, 'topic_brief', 'v1', 'short_video_9x16_v1',
                        '{}'::jsonb, 'racing', %s, %s, 'pending', %s)
                """,
                (
                    step_id,
                    project.id,
                    run.id,
                    definition_digest(short_video_v1_definition()),
                    "f" * 64,
                    job_id,
                    NOW,
                ),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        terminal = executor.submit(finish_run)
        raw_insert = executor.submit(insert_raw_step)
        assert insert_started.wait(timeout=5)
        allow_terminal_commit.set()
        terminal.result(timeout=5)
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="terminal"):
            raw_insert.result(timeout=5)


@pytest.mark.parametrize(
    "change",
    ({"step_version": "v999"}, {"profile_version": "other_profile"}),
)
def test_database_binds_step_version_and_profile_to_definition(
    database: Database, change: dict[str, str]
) -> None:
    project, fact, run = setup_run(database)
    with pytest.raises(ValueError, match="PipelineDefinition"):
        StepRunRepository(database).reserve_and_enqueue(
            replace(topic_spec(project, fact, run), **change), "wrong-definition", NOW, 3
        )


def test_repository_validates_exact_definition_bindings_and_outputs(database: Database) -> None:
    project, fact, run = setup_run(database)
    valid = topic_spec(project, fact, run)
    invalid_specs = (
        replace(
            valid,
            input_refs=(BoundArtifactRef("other", LogicalArtifactRef(fact.ref, "fact_card")),),
        ),
        replace(
            valid,
            input_refs=(
                BoundArtifactRef("fact_card", LogicalArtifactRef(fact.ref, "other_input")),
            ),
        ),
        replace(
            valid,
            outputs=(StepOutputSpec("topic_brief", ArtifactKind.TOPIC_BRIEF, "other_output"),),
        ),
    )
    repository = StepRunRepository(database)
    for index, invalid in enumerate(invalid_specs):
        with pytest.raises(ValueError):
            repository.reserve_and_enqueue(invalid, f"invalid-{index}", NOW, 3)


def complete_step(database: Database, step: object, sha_char: str) -> LogicalArtifactRef:
    reservation = step.output_reservations[0]
    artifact = create_artifact(
        database,
        step.spec.project_id,
        reservation.output.kind,
        artifact_id=reservation.artifact_id,
        version=reservation.version,
        sha=sha_char * 64,
    )
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE step_runs SET status = 'succeeded', finished_at = %s WHERE id = %s
            """,
            (NOW + timedelta(seconds=1), step.id),
        )
        cursor.execute(
            "UPDATE jobs SET status = 'succeeded', updated_at = %s WHERE id = %s",
            (NOW + timedelta(seconds=1), step.job_id),
        )
    return LogicalArtifactRef(artifact.ref, reservation.output.logical_key)


def test_upstream_change_invalidates_only_exact_dependent_subgraph(database: Database) -> None:
    project, fact, run = setup_run(database)
    repository = StepRunRepository(database)
    topic = repository.reserve_and_enqueue(topic_spec(project, fact, run), "topic", NOW, 3)
    topic_ref = complete_step(database, topic, "b")
    script_spec = StepRunSpec(
        project.id,
        run.id,
        StepKind.SCRIPT,
        "v1",
        (BoundArtifactRef("topic_brief", topic_ref),),
        (StepOutputSpec("script", ArtifactKind.SCRIPT, "script"),),
    )
    script = repository.reserve_and_enqueue(script_spec, "script", NOW, 3)
    script_ref = complete_step(database, script, "c")
    storyboard = repository.reserve_and_enqueue(
        StepRunSpec(
            project.id,
            run.id,
            StepKind.STORYBOARD,
            "v1",
            (BoundArtifactRef("script", script_ref),),
            (StepOutputSpec("storyboard", ArtifactKind.STORYBOARD, "storyboard"),),
        ),
        "storyboard",
        NOW,
        3,
    )
    complete_step(database, storyboard, "d")
    unrelated_script = create_artifact(database, project.id, ArtifactKind.SCRIPT)
    voice = repository.reserve_and_enqueue(
        StepRunSpec(
            project.id,
            run.id,
            StepKind.VOICEOVER,
            "v1",
            (BoundArtifactRef("script", LogicalArtifactRef(unrelated_script.ref, "script")),),
            (StepOutputSpec("voiceover", ArtifactKind.VOICEOVER, "voiceover"),),
        ),
        "unrelated",
        NOW,
        3,
    )
    complete_step(database, voice, "e")

    affected = repository.invalidate_dependents(
        run.id, (topic_ref,), NOW + timedelta(seconds=3), "upstream_changed"
    )
    assert set(affected) == {script.id, storyboard.id}
    assert repository.get(script.id).status.value == "invalidated"
    assert repository.get(storyboard.id).status.value == "invalidated"
    assert repository.get(topic.id).status.value == "succeeded"
    assert repository.get(voice.id).status.value == "succeeded"

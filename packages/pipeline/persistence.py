from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4, uuid5

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from apps.common.database import Database
from packages.domain import (
    ArtifactKind,
    ArtifactRef,
    BoundArtifactRef,
    EntityNotFoundError,
    LogicalArtifactRef,
    PipelineRun,
    PipelineRunStatus,
    StepKind,
    StepOutputReservation,
    StepOutputSpec,
    StepRun,
    StepRunSpec,
    StepRunStatus,
    validate_fanout_item_rerun,
    validate_step_rerun,
)
from packages.domain.types import FrozenJsonValue
from packages.pipeline.contracts import (
    ArtifactSlotDefinition,
    OutputCardinality,
    PipelineDefinition,
    StepDefinition,
    StepInputDefinition,
    StepTrigger,
    canonical_step_request_hash,
    definition_digest,
    definition_manifest,
)

PIPELINE_STEP_JOB_KIND = "pipeline_step"


class StepRunConflict(Exception):
    """An idempotency key or active fingerprint conflicts with persisted state."""


@dataclass(frozen=True, slots=True)
class PipelineDefinitionRecord:
    digest: str
    pipeline_kind: str
    pipeline_version: str
    profile_version: str
    manifest: Mapping[str, Any]
    created_at: datetime


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _thaw(value: FrozenJsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _definition_from_manifest(manifest: Mapping[str, Any]) -> PipelineDefinition:
    def slot(raw: Sequence[Any]) -> ArtifactSlotDefinition:
        return ArtifactSlotDefinition(
            str(raw[0]),
            ArtifactKind(str(raw[1])),
            str(raw[2]),
            OutputCardinality(str(raw[3])),
            tuple(str(item) for item in raw[4]),
        )

    steps = tuple(
        StepDefinition(
            StepKind(str(raw["kind"])),
            str(raw["version"]),
            tuple(
                StepInputDefinition(
                    str(item[0]),
                    ArtifactKind(str(item[1])),
                    StepKind(str(item[2])) if item[2] is not None else None,
                    str(item[3]),
                )
                for item in raw["inputs"]
            ),
            tuple(slot(item) for item in raw["outputs"]),
            StepTrigger(str(raw["trigger"])),
            bool(raw["completion"]),
        )
        for raw in manifest["steps"]
    )
    return PipelineDefinition(
        str(manifest["kind"]),
        str(manifest["version"]),
        str(manifest["profile_version"]),
        tuple(slot(item) for item in manifest["external_inputs"]),
        steps,
    )


class PipelineDefinitionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def register(self, definition: PipelineDefinition, created_at: datetime) -> str:
        digest = definition_digest(definition)
        with self._database.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pipeline_definitions
                    (digest, pipeline_kind, pipeline_version, profile_version, manifest, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (digest) DO NOTHING
                """,
                (
                    digest,
                    definition.kind,
                    definition.version,
                    definition.profile_version,
                    Jsonb(definition_manifest(definition)),
                    _utc(created_at),
                ),
            )
            cursor.executemany(
                """
                INSERT INTO pipeline_definition_steps
                    (definition_digest, step_kind, step_version)
                VALUES (%s, %s, %s)
                ON CONFLICT (definition_digest, step_kind) DO NOTHING
                """,
                [(digest, step.kind.value, step.version) for step in definition.steps],
            )
        return digest

    def get(self, digest: str) -> PipelineDefinitionRecord:
        with (
            self._database.connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SELECT * FROM pipeline_definitions WHERE digest = %s", (digest,))
            row = cursor.fetchone()
        if row is None:
            raise EntityNotFoundError(f"pipeline definition not found: {digest}")
        return PipelineDefinitionRecord(
            row["digest"],
            row["pipeline_kind"],
            row["pipeline_version"],
            row["profile_version"],
            row["manifest"],
            row["created_at"],
        )


class PipelineRunRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, run: PipelineRun, definition_digest_value: str) -> None:
        with self._database.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pipeline_runs
                    (id, project_id, definition_digest, pipeline_kind, pipeline_version,
                     profile_version, status, created_at, started_at, finished_at,
                     error_class, invalidated_reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run.id,
                    run.project_id,
                    definition_digest_value,
                    run.pipeline_kind,
                    run.pipeline_version,
                    run.profile_version,
                    run.status.value,
                    run.created_at,
                    run.started_at,
                    run.finished_at,
                    run.error_class,
                    run.invalidated_reason,
                ),
            )
            cursor.executemany(
                """
                INSERT INTO pipeline_run_inputs
                    (pipeline_run_id, project_id, position, logical_key, artifact_id,
                     artifact_version,
                     artifact_kind, artifact_sha256)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        run.id,
                        run.project_id,
                        position,
                        ref.logical_key,
                        ref.artifact_id,
                        ref.version,
                        ref.kind.value,
                        ref.sha256,
                    )
                    for position, ref in enumerate(run.input_refs)
                ],
            )

    def get(self, run_id: UUID) -> PipelineRun:
        with (
            self._database.connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SELECT * FROM pipeline_runs WHERE id = %s", (run_id,))
            row = cursor.fetchone()
            if row is None:
                raise EntityNotFoundError(f"pipeline run not found: {run_id}")
            cursor.execute(
                """
                SELECT * FROM pipeline_run_inputs
                WHERE pipeline_run_id = %s ORDER BY position
                """,
                (run_id,),
            )
            inputs = tuple(_logical_ref(item) for item in cursor.fetchall())
        return PipelineRun(
            row["id"],
            row["project_id"],
            row["pipeline_kind"],
            row["pipeline_version"],
            row["profile_version"],
            inputs,
            PipelineRunStatus(row["status"]),
            row["created_at"],
            row["started_at"],
            row["finished_at"],
            row["error_class"],
            row["invalidated_reason"],
        )


class StepRunRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def reserve_and_enqueue(
        self,
        spec: StepRunSpec,
        idempotency_key: str,
        now: datetime,
        max_attempts: int,
    ) -> StepRun:
        key = idempotency_key.strip()
        if not key or len(key) > 200:
            raise ValueError("idempotency_key must contain 1 to 200 characters")
        if isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        now = _utc(now)
        request_hash = canonical_step_request_hash(spec)
        with (
            self._database.connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT * FROM pipeline_runs WHERE id = %s FOR UPDATE", (spec.pipeline_run_id,)
            )
            pipeline_row = cursor.fetchone()
            if pipeline_row is None:
                raise EntityNotFoundError(f"pipeline run not found: {spec.pipeline_run_id}")
            if pipeline_row["project_id"] != spec.project_id:
                raise ValueError("StepRunSpec project does not own PipelineRun")
            cursor.execute(
                "SELECT manifest FROM pipeline_definitions WHERE digest = %s",
                (pipeline_row["definition_digest"],),
            )
            definition_row = cursor.fetchone()
            assert definition_row is not None
            definition = _definition_from_manifest(definition_row["manifest"])
            validation_spec = spec
            if spec.rerun_of_step_run_id is not None:
                cursor.execute(
                    "SELECT status FROM step_runs WHERE id = %s",
                    (spec.rerun_of_step_run_id,),
                )
                rerun_parent = cursor.fetchone()
                if rerun_parent is not None and rerun_parent["status"] != "succeeded":
                    validation_spec = replace(spec, rerun_of_step_run_id=None)
            definition.validate_step_spec(validation_spec)
            cursor.execute(
                """
                SELECT id FROM step_runs
                WHERE pipeline_run_id = %s AND step_kind = %s AND idempotency_key = %s
                """,
                (spec.pipeline_run_id, spec.step_kind.value, key),
            )
            existing = cursor.fetchone()
            if existing is not None:
                stored = self._get_with_cursor(cursor, existing["id"])
                if stored.request_hash != request_hash:
                    raise StepRunConflict("idempotency key was reused with a different request")
                return stored

            step_run_id = uuid4()
            job_id = uuid4()
            reservations = tuple(
                self._reserve_output(cursor, spec.project_id, output, now)
                for output in spec.outputs
            )
            candidate = StepRun(
                step_run_id,
                spec,
                key,
                request_hash,
                job_id,
                reservations,
                StepRunStatus.PENDING,
                now,
            )
            if spec.rerun_of_step_run_id is not None:
                parent = self._get_with_cursor(cursor, spec.rerun_of_step_run_id)
                if parent.status is StepRunStatus.SUCCEEDED:
                    validate_fanout_item_rerun(parent, candidate)
                else:
                    validate_step_rerun(parent, candidate)
            cursor.execute(
                """
                INSERT INTO jobs
                    (id, project_id, kind, payload, status, attempt, max_attempts,
                     available_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, 'queued', 0, %s, %s, %s, %s)
                """,
                (
                    job_id,
                    spec.project_id,
                    PIPELINE_STEP_JOB_KIND,
                    Jsonb({"step_run_id": str(step_run_id)}),
                    max_attempts,
                    now,
                    now,
                    now,
                ),
            )
            try:
                cursor.execute(
                    """
                    INSERT INTO step_runs
                        (id, project_id, pipeline_run_id, definition_digest,
                         step_kind, step_version,
                         profile_version, parameters, idempotency_key, request_hash, job_id,
                         rerun_of_step_run_id, status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            'pending', %s)
                    """,
                    (
                        step_run_id,
                        spec.project_id,
                        spec.pipeline_run_id,
                        pipeline_row["definition_digest"],
                        spec.step_kind.value,
                        spec.step_version,
                        spec.profile_version,
                        Jsonb({key: _thaw(value) for key, value in spec.parameters.items()}),
                        key,
                        request_hash,
                        job_id,
                        spec.rerun_of_step_run_id,
                        now,
                    ),
                )
            except errors.UniqueViolation as error:
                raise StepRunConflict(
                    "a StepRun already has this fingerprint; use a new explicit rerun"
                ) from error
            self._insert_refs(
                cursor, "step_run_inputs", step_run_id, spec.project_id, spec.input_refs
            )
            self._insert_refs(
                cursor,
                "step_run_preserved_outputs",
                step_run_id,
                spec.project_id,
                spec.preserved_output_refs,
            )
            cursor.executemany(
                """
                INSERT INTO step_output_reservations
                    (step_run_id, project_id, position, slot_name, item_key, logical_key,
                     kind, artifact_id, artifact_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        step_run_id,
                        spec.project_id,
                        position,
                        item.output.slot_name,
                        item.output.item_key,
                        item.output.logical_key,
                        item.output.kind.value,
                        item.artifact_id,
                        item.version,
                    )
                    for position, item in enumerate(reservations)
                ],
            )
            return candidate

    def get(self, step_run_id: UUID) -> StepRun:
        with (
            self._database.connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            return self._get_with_cursor(cursor, step_run_id)

    def invalidate_dependents(
        self,
        pipeline_run_id: UUID,
        changed_refs: Sequence[LogicalArtifactRef],
        occurred_at: datetime,
        reason: str,
    ) -> tuple[UUID, ...]:
        if not changed_refs or not reason.strip():
            raise ValueError("changed_refs and reason are required")
        values = [(ref.artifact_id, ref.version) for ref in changed_refs]
        with self._database.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH RECURSIVE affected(id) AS (
                    SELECT DISTINCT sr.id
                    FROM step_runs sr
                    JOIN step_run_inputs input ON input.step_run_id = sr.id
                    WHERE sr.pipeline_run_id = %s
                      AND (input.artifact_id, input.artifact_version) IN (
                          SELECT * FROM unnest(%s::uuid[], %s::integer[])
                      )
                    UNION
                    SELECT DISTINCT child.id
                    FROM affected parent
                    JOIN step_output_reservations output ON output.step_run_id = parent.id
                    JOIN step_run_inputs input
                      ON (input.artifact_id, input.artifact_version) =
                         (output.artifact_id, output.artifact_version)
                    JOIN step_runs child ON child.id = input.step_run_id
                    WHERE child.pipeline_run_id = %s
                )
                UPDATE step_runs
                SET status = 'invalidated', finished_at = %s, invalidated_reason = %s
                WHERE id IN (SELECT id FROM affected) AND status = 'succeeded'
                RETURNING id
                """,
                (
                    pipeline_run_id,
                    [item[0] for item in values],
                    [item[1] for item in values],
                    pipeline_run_id,
                    _utc(occurred_at),
                    reason.strip(),
                ),
            )
            return tuple(sorted((row[0] for row in cursor.fetchall()), key=str))

    @staticmethod
    def _reserve_output(
        cursor: Any, project_id: UUID, output: StepOutputSpec, now: datetime
    ) -> StepOutputReservation:
        artifact_id = uuid5(project_id, f"artifact:{output.logical_key}")
        cursor.execute(
            """
            INSERT INTO logical_artifact_streams
                (project_id, logical_key, kind, artifact_id, created_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (project_id, logical_key) DO NOTHING
            """,
            (project_id, output.logical_key, output.kind.value, artifact_id, now),
        )
        cursor.execute(
            """
            SELECT * FROM logical_artifact_streams
            WHERE project_id = %s AND logical_key = %s FOR UPDATE
            """,
            (project_id, output.logical_key),
        )
        stream = cursor.fetchone()
        assert stream is not None
        if stream["kind"] != output.kind.value or stream["artifact_id"] != artifact_id:
            raise StepRunConflict("logical output identity conflicts with its persisted stream")
        cursor.execute(
            """
            SELECT GREATEST(
                COALESCE((SELECT MAX(artifact_version) FROM step_output_reservations
                          WHERE project_id = %s AND logical_key = %s), 0),
                COALESCE((SELECT MAX(version) FROM artifacts WHERE id = %s), 0)
            ) + 1 AS next_version
            """,
            (project_id, output.logical_key, artifact_id),
        )
        version_row = cursor.fetchone()
        assert version_row is not None
        version = version_row["next_version"]
        return StepOutputReservation(output, artifact_id, version)

    @staticmethod
    def _insert_refs(
        cursor: Any,
        table: str,
        step_run_id: UUID,
        project_id: UUID,
        refs: Sequence[Any],
    ) -> None:
        if not refs:
            return
        if table == "step_run_inputs":
            input_rows = [
                (
                    step_run_id,
                    project_id,
                    position,
                    item.binding_name,
                    item.artifact.logical_key,
                    item.artifact.artifact_id,
                    item.artifact.version,
                    item.artifact.kind.value,
                    item.artifact.sha256,
                )
                for position, item in enumerate(refs)
            ]
            cursor.executemany(
                """
                INSERT INTO step_run_inputs
                    (step_run_id, project_id, position, binding_name, logical_key, artifact_id,
                     artifact_version, artifact_kind, artifact_sha256)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                input_rows,
            )
        else:
            preserved_rows = [
                (
                    step_run_id,
                    project_id,
                    position,
                    item.logical_key,
                    item.artifact_id,
                    item.version,
                    item.kind.value,
                    item.sha256,
                )
                for position, item in enumerate(refs)
            ]
            cursor.executemany(
                """
                INSERT INTO step_run_preserved_outputs
                    (step_run_id, project_id, position, logical_key, artifact_id, artifact_version,
                     artifact_kind, artifact_sha256)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                preserved_rows,
            )

    def _get_with_cursor(self, cursor: Any, step_run_id: UUID) -> StepRun:
        cursor.execute("SELECT * FROM step_runs WHERE id = %s", (step_run_id,))
        row = cursor.fetchone()
        if row is None:
            raise EntityNotFoundError(f"step run not found: {step_run_id}")
        cursor.execute(
            "SELECT * FROM step_run_inputs WHERE step_run_id = %s ORDER BY position", (step_run_id,)
        )
        inputs = tuple(
            BoundArtifactRef(item["binding_name"], _logical_ref(item)) for item in cursor.fetchall()
        )
        cursor.execute(
            "SELECT * FROM step_run_preserved_outputs WHERE step_run_id = %s ORDER BY position",
            (step_run_id,),
        )
        preserved = tuple(_logical_ref(item) for item in cursor.fetchall())
        cursor.execute(
            "SELECT * FROM step_output_reservations WHERE step_run_id = %s ORDER BY position",
            (step_run_id,),
        )
        reservations = tuple(
            StepOutputReservation(
                StepOutputSpec(
                    item["slot_name"],
                    ArtifactKind(item["kind"]),
                    item["logical_key"],
                    item["item_key"],
                ),
                item["artifact_id"],
                item["artifact_version"],
            )
            for item in cursor.fetchall()
        )
        output_refs: tuple[LogicalArtifactRef, ...] = ()
        if row["status"] == StepRunStatus.SUCCEEDED.value:
            cursor.execute(
                """
                SELECT reservation.logical_key, artifact.id AS artifact_id,
                       artifact.version AS artifact_version,
                       artifact.kind AS artifact_kind,
                       artifact.sha256 AS artifact_sha256
                FROM step_output_reservations reservation
                JOIN artifacts artifact
                  ON (artifact.id, artifact.version) =
                     (reservation.artifact_id, reservation.artifact_version)
                WHERE reservation.step_run_id = %s
                ORDER BY reservation.position
                """,
                (step_run_id,),
            )
            output_refs = tuple(_logical_ref(item) for item in cursor.fetchall())
        spec = StepRunSpec(
            row["project_id"],
            row["pipeline_run_id"],
            StepKind(row["step_kind"]),
            row["step_version"],
            inputs,
            tuple(item.output for item in reservations),
            row["parameters"],
            row["profile_version"],
            row["rerun_of_step_run_id"],
            preserved,
        )
        return StepRun(
            row["id"],
            spec,
            row["idempotency_key"],
            row["request_hash"],
            row["job_id"],
            reservations,
            StepRunStatus(row["status"]),
            row["created_at"],
            output_refs,
            row["finished_at"],
            row["error_class"],
            row["invalidated_reason"],
        )


def _logical_ref(row: Mapping[str, Any]) -> LogicalArtifactRef:
    return LogicalArtifactRef(
        ArtifactRef(
            row["artifact_id"],
            row["artifact_version"],
            ArtifactKind(row["artifact_kind"]),
            row["artifact_sha256"],
        ),
        row["logical_key"],
    )

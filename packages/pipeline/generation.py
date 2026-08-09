from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4, uuid5

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from apps.common.database import Database
from packages.domain import (
    Artifact,
    ArtifactKind,
    ContentProject,
    EntityNotFoundError,
    FailedStage,
    InvalidStateTransition,
    PermanentError,
    ProjectStatus,
    Source,
    SourceCitation,
    transition_project,
)
from packages.pipeline.evidence import validate_artifact_evidence

FACT_CARD_JOB_KIND = "generate_fact_card"
FACT_CARD_ID_NAME = "artifact:fact_card"


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


class GenerationRequestConflict(PermanentError):
    """An idempotency key was reused with a different canonical request."""


class EvidenceSourceError(PermanentError):
    """Requested Sources cannot produce the required deterministic citations."""


@dataclass(frozen=True, slots=True)
class FactCardGenerationSpec:
    project_id: UUID
    source_ids: tuple[UUID, ...]
    template_version: str
    budget_units: int

    def __post_init__(self) -> None:
        source_ids = tuple(self.source_ids)
        if not source_ids:
            raise ValueError("source_ids must not be empty")
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("source_ids must not contain duplicates")
        if not self.template_version.strip():
            raise ValueError("template_version is required")
        if self.budget_units < 1:
            raise ValueError("budget_units must be positive")
        object.__setattr__(self, "source_ids", tuple(sorted(source_ids, key=str)))


@dataclass(frozen=True, slots=True)
class GenerationReservation:
    spec: FactCardGenerationSpec
    idempotency_key: str
    request_hash: str
    job_id: UUID
    artifact_id: UUID
    artifact_version: int
    project_revision: int
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if len(self.request_hash) != 64 or any(
            char not in "0123456789abcdef" for char in self.request_hash
        ):
            raise ValueError("request_hash must be a lowercase SHA-256 digest")
        if self.artifact_version < 1 or self.project_revision < 1:
            raise ValueError("reservation versions must be positive")
        object.__setattr__(self, "created_at", _utc(self.created_at))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def canonical_request_hash(spec: FactCardGenerationSpec) -> str:
    content = json.dumps(
        {
            "budget_units": spec.budget_units,
            "project_id": str(spec.project_id),
            "source_ids": [str(source_id) for source_id in spec.source_ids],
            "template_version": spec.template_version,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def fact_card_artifact_id(project_id: UUID) -> UUID:
    return uuid5(project_id, FACT_CARD_ID_NAME)


def citations_for_sources(sources: Sequence[Source]) -> tuple[SourceCitation, ...]:
    if not sources:
        raise EvidenceSourceError("sources must not be empty")
    source_ids = [source.id for source in sources]
    if len(set(source_ids)) != len(source_ids):
        raise EvidenceSourceError("sources must not contain duplicates")
    if any(not source.excerpts for source in sources):
        raise EvidenceSourceError("every source must contain at least one excerpt")
    return tuple(
        sorted(
            (
                SourceCitation(source.id, excerpt.id, source.sha256)
                for source in sources
                for excerpt in source.excerpts
            ),
            key=lambda citation: (str(citation.source_id), str(citation.excerpt_id)),
        )
    )


class GenerationRequestRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def reserve_and_enqueue(
        self,
        spec: FactCardGenerationSpec,
        idempotency_key: str,
        now: datetime,
        max_attempts: int,
    ) -> GenerationReservation:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("idempotency_key must contain 1 to 200 characters")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        now = _utc(now)
        request_hash = canonical_request_hash(spec)
        artifact_id = fact_card_artifact_id(spec.project_id)

        with (
            self._database.connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SELECT * FROM projects WHERE id = %s FOR UPDATE", (spec.project_id,))
            project_row = cursor.fetchone()
            if project_row is None:
                raise EntityNotFoundError(f"project not found: {spec.project_id}")

            cursor.execute(
                """
                SELECT * FROM generation_requests
                WHERE project_id = %s AND idempotency_key = %s
                """,
                (spec.project_id, idempotency_key),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise GenerationRequestConflict(
                        "idempotency key was reused with a different request"
                    )
                return _reservation_from_row(existing)

            project = ContentProject(
                id=project_row["id"],
                title=project_row["title"],
                status=ProjectStatus(project_row["status"]),
                created_at=project_row["created_at"],
                updated_at=project_row["updated_at"],
                revision=project_row["revision"],
                failed_stage=(
                    FailedStage(project_row["failed_stage"])
                    if project_row["failed_stage"]
                    else None
                ),
            )
            if project.status is not ProjectStatus.DRAFT and not (
                project.status is ProjectStatus.FAILED
                and project.failed_stage is FailedStage.GENERATION
            ):
                raise InvalidStateTransition(
                    f"cannot request generation while project is {project.status}"
                )
            updated_project = transition_project(project, ProjectStatus.GENERATING, occurred_at=now)

            cursor.execute(
                """
                SELECT GREATEST(
                    COALESCE((SELECT MAX(version) FROM artifacts WHERE id = %s), 0),
                    COALESCE((
                        SELECT MAX(artifact_version) FROM generation_requests
                        WHERE artifact_id = %s
                    ), 0)
                ) + 1 AS next_version
                """,
                (artifact_id, artifact_id),
            )
            version_row = cursor.fetchone()
            assert version_row is not None
            artifact_version = version_row["next_version"]
            job_id = uuid4()
            payload = {
                "budget_units": spec.budget_units,
                "output_artifact_id": str(artifact_id),
                "output_version": artifact_version,
                "project_id": str(spec.project_id),
                "source_ids": [str(source_id) for source_id in spec.source_ids],
                "template_version": spec.template_version,
            }
            cursor.execute(
                """
                UPDATE projects
                SET status = 'generating', revision = %s, updated_at = %s, failed_stage = NULL
                WHERE id = %s AND revision = %s
                """,
                (
                    updated_project.revision,
                    updated_project.updated_at,
                    spec.project_id,
                    project.revision,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidStateTransition("project changed while reserving generation")
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
                    FACT_CARD_JOB_KIND,
                    Jsonb(payload),
                    max_attempts,
                    now,
                    now,
                    now,
                ),
            )
            cursor.execute(
                """
                INSERT INTO generation_requests
                    (project_id, idempotency_key, request_hash, job_id, artifact_id,
                     artifact_version, source_ids, template_version, budget_units,
                     project_revision, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    spec.project_id,
                    idempotency_key,
                    request_hash,
                    job_id,
                    artifact_id,
                    artifact_version,
                    list(spec.source_ids),
                    spec.template_version,
                    spec.budget_units,
                    updated_project.revision,
                    now,
                ),
            )
            reserved = cursor.fetchone()
            assert reserved is not None
            return _reservation_from_row(reserved)

    def get_by_job_id(self, job_id: UUID) -> GenerationReservation:
        with (
            self._database.connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SELECT * FROM generation_requests WHERE job_id = %s", (job_id,))
            row = cursor.fetchone()
        if row is None:
            raise EntityNotFoundError(f"generation reservation not found for job: {job_id}")
        return _reservation_from_row(row)


class GenerationTerminalRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def succeed(
        self,
        job_id: UUID,
        worker_id: str,
        now: datetime,
        artifact: Artifact,
        sources_by_id: Mapping[UUID, Source],
    ) -> None:
        report = validate_artifact_evidence(artifact, sources_by_id)
        if not report.valid:
            raise EvidenceSourceError("artifact evidence validation failed")
        with (
            self._database.connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            reservation, project = self._lock_context(cursor, job_id, worker_id, _utc(now))
            self._validate_artifact(reservation, artifact)
            self._insert_artifact(cursor, artifact)
            cursor.execute(
                """
                INSERT INTO project_current_artifacts
                    (project_id, kind, artifact_id, artifact_version)
                VALUES (%s, 'fact_card', %s, %s)
                ON CONFLICT (project_id, kind) DO UPDATE
                SET artifact_id = EXCLUDED.artifact_id,
                    artifact_version = EXCLUDED.artifact_version
                """,
                (artifact.project_id, artifact.ref.artifact_id, artifact.ref.version),
            )
            self._finish_project(cursor, project, ProjectStatus.REVIEW_REQUIRED, now)
            self._finish_job(cursor, job_id, worker_id, now, "succeeded", None)

    def fail_permanently(
        self,
        job_id: UUID,
        worker_id: str,
        now: datetime,
        error_class: str,
        artifact: Artifact | None = None,
    ) -> None:
        if not error_class.strip():
            raise ValueError("error_class is required")
        with (
            self._database.connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            reservation, project = self._lock_context(cursor, job_id, worker_id, _utc(now))
            if artifact is not None:
                self._validate_artifact(reservation, artifact)
                self._insert_artifact(cursor, artifact)
            self._finish_project(cursor, project, ProjectStatus.FAILED, now)
            self._finish_job(cursor, job_id, worker_id, now, "failed", error_class)

    @staticmethod
    def _lock_context(
        cursor: Any, job_id: UUID, worker_id: str, now: datetime
    ) -> tuple[GenerationReservation, ContentProject]:
        cursor.execute(
            """
            SELECT j.*, g.idempotency_key, g.request_hash, g.artifact_id,
                   g.artifact_version, g.source_ids, g.template_version,
                   g.budget_units, g.project_revision, g.created_at AS request_created_at
            FROM jobs j JOIN generation_requests g ON g.job_id = j.id
            WHERE j.id = %s FOR UPDATE OF j, g
            """,
            (job_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise EntityNotFoundError(f"generation job not found: {job_id}")
        if (
            row["status"] != "running"
            or row["lease_owner"] != worker_id
            or row["lease_expires_at"] <= now
        ):
            from packages.pipeline.jobs import LostLeaseError

            raise LostLeaseError(f"live lease not held for job {job_id}")
        cursor.execute("SELECT * FROM projects WHERE id = %s FOR UPDATE", (row["project_id"],))
        project_row = cursor.fetchone()
        if project_row is None:
            raise EntityNotFoundError(f"project not found: {row['project_id']}")
        project = ContentProject(
            project_row["id"],
            project_row["title"],
            ProjectStatus(project_row["status"]),
            project_row["created_at"],
            project_row["updated_at"],
            project_row["revision"],
            FailedStage(project_row["failed_stage"]) if project_row["failed_stage"] else None,
        )
        if (
            project.status is not ProjectStatus.GENERATING
            or project.revision != row["project_revision"]
        ):
            raise InvalidStateTransition("generation reservation no longer matches project")
        reservation = _reservation_from_row(
            {
                "project_id": row["project_id"],
                "source_ids": row["source_ids"],
                "template_version": row["template_version"],
                "budget_units": row["budget_units"],
                "idempotency_key": row["idempotency_key"],
                "request_hash": row["request_hash"],
                "job_id": row["id"],
                "artifact_id": row["artifact_id"],
                "artifact_version": row["artifact_version"],
                "project_revision": row["project_revision"],
                "created_at": row["request_created_at"],
            }
        )
        return reservation, project

    @staticmethod
    def _validate_artifact(reservation: GenerationReservation, artifact: Artifact) -> None:
        if (
            artifact.project_id != reservation.spec.project_id
            or artifact.ref.kind is not ArtifactKind.FACT_CARD
            or artifact.ref.artifact_id != reservation.artifact_id
            or artifact.ref.version != reservation.artifact_version
        ):
            raise EvidenceSourceError("artifact does not match generation reservation")

    @staticmethod
    def _insert_artifact(cursor: Any, artifact: Artifact) -> None:
        cursor.execute(
            """INSERT INTO artifacts
            (id, version, kind, sha256, project_id, storage_path, created_at,
             created_by, adapter, metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                artifact.ref.artifact_id,
                artifact.ref.version,
                artifact.ref.kind.value,
                artifact.ref.sha256,
                artifact.project_id,
                artifact.storage_path,
                artifact.created_at,
                artifact.created_by,
                artifact.adapter,
                Jsonb(_thaw_json(artifact.metadata)),
            ),
        )
        cursor.executemany(
            """INSERT INTO artifact_upstream
            (project_id, artifact_id, artifact_version, position, upstream_id, upstream_version)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            [
                (
                    artifact.project_id,
                    artifact.ref.artifact_id,
                    artifact.ref.version,
                    pos,
                    ref.artifact_id,
                    ref.version,
                )
                for pos, ref in enumerate(artifact.upstream)
            ],
        )
        cursor.executemany(
            """INSERT INTO artifact_citations
            (artifact_id, artifact_version, position, source_id, excerpt_id, source_sha256)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            [
                (
                    artifact.ref.artifact_id,
                    artifact.ref.version,
                    pos,
                    citation.source_id,
                    citation.excerpt_id,
                    citation.source_sha256,
                )
                for pos, citation in enumerate(artifact.citations)
            ],
        )

    @staticmethod
    def _finish_project(
        cursor: Any, project: ContentProject, target: ProjectStatus, now: datetime
    ) -> None:
        updated = transition_project(project, target, occurred_at=_utc(now))
        cursor.execute(
            """UPDATE projects SET status=%s, revision=%s, updated_at=%s, failed_stage=%s
            WHERE id=%s AND revision=%s AND status='generating'""",
            (
                updated.status.value,
                updated.revision,
                updated.updated_at,
                updated.failed_stage.value if updated.failed_stage else None,
                project.id,
                project.revision,
            ),
        )
        if cursor.rowcount != 1:
            raise InvalidStateTransition("project changed during generation terminal transaction")

    @staticmethod
    def _finish_job(
        cursor: Any,
        job_id: UUID,
        worker_id: str,
        now: datetime,
        status: str,
        error_class: str | None,
    ) -> None:
        cursor.execute(
            """UPDATE jobs SET status=%s, lease_owner=NULL, lease_expires_at=NULL,
            last_error=%s, updated_at=%s WHERE id=%s AND status='running'
            AND lease_owner=%s AND lease_expires_at>%s""",
            (status, error_class, _utc(now), job_id, worker_id, _utc(now)),
        )
        if cursor.rowcount != 1:
            from packages.pipeline.jobs import LostLeaseError

            raise LostLeaseError(f"live lease not held for job {job_id}")


def _reservation_from_row(row: Mapping[str, Any]) -> GenerationReservation:
    return GenerationReservation(
        spec=FactCardGenerationSpec(
            project_id=row["project_id"],
            source_ids=tuple(row["source_ids"]),
            template_version=row["template_version"],
            budget_units=row["budget_units"],
        ),
        idempotency_key=row["idempotency_key"],
        request_hash=row["request_hash"],
        job_id=row["job_id"],
        artifact_id=row["artifact_id"],
        artifact_version=row["artifact_version"],
        project_revision=row["project_revision"],
        created_at=row["created_at"],
    )

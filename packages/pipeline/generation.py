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

FACT_CARD_JOB_KIND = "generate_fact_card"
FACT_CARD_ID_NAME = "artifact:fact_card"


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

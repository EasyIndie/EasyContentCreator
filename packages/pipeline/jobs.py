from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from apps.common.database import Database
from packages.domain.errors import RetryableError
from packages.domain.types import FrozenJsonValue, freeze_json_mapping


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class LostLeaseError(Exception):
    """The worker no longer owns a live lease for the requested Job."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _lease_delta(value: timedelta) -> timedelta:
    if value <= timedelta(0):
        raise ValueError("lease_for must be positive")
    return value


def _thaw_json(value: FrozenJsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Job:
    id: UUID
    project_id: UUID
    kind: str
    payload: Mapping[str, FrozenJsonValue]
    status: JobStatus
    attempt: int
    max_attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("job kind is required")
        if self.attempt < 0 or self.max_attempts < 1 or self.attempt > self.max_attempts:
            raise ValueError("job attempts are invalid")
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError("lease owner and expiry must be set together")
        if self.status is JobStatus.RUNNING and self.lease_owner is None:
            raise ValueError("running job requires a lease")
        if self.status is not JobStatus.RUNNING and self.lease_owner is not None:
            raise ValueError("only running job may hold a lease")
        object.__setattr__(self, "payload", freeze_json_mapping(dict(self.payload)))
        object.__setattr__(self, "available_at", _utc(self.available_at))
        object.__setattr__(self, "created_at", _utc(self.created_at))
        object.__setattr__(self, "updated_at", _utc(self.updated_at))
        if self.lease_expires_at is not None:
            object.__setattr__(self, "lease_expires_at", _utc(self.lease_expires_at))


@dataclass(frozen=True, slots=True)
class JobStore:
    database: Database = field(repr=False)

    def enqueue(
        self,
        kind: str,
        project_id: UUID,
        payload: Mapping[str, FrozenJsonValue],
        max_attempts: int,
        available_at: datetime,
    ) -> UUID:
        if not kind.strip():
            raise ValueError("job kind is required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        available_at = _utc(available_at)
        job_id = uuid4()
        with self.database.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO jobs
                    (id, project_id, kind, payload, status, attempt, max_attempts,
                     available_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, 'queued', 0, %s, %s,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    job_id,
                    project_id,
                    kind,
                    Jsonb(_thaw_json(freeze_json_mapping(dict(payload)))),
                    max_attempts,
                    available_at,
                ),
            )
        return job_id

    def claim(self, worker_id: str, now: datetime, lease_for: timedelta) -> Job | None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        now = _utc(now)
        lease_expires_at = now + _lease_delta(lease_for)
        with (
            self.database.connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                """
                UPDATE jobs
                SET status = 'failed', lease_owner = NULL, lease_expires_at = NULL,
                    last_error = 'LeaseExpired', updated_at = %(now)s
                WHERE status = 'running' AND lease_expires_at <= %(now)s
                  AND attempt >= max_attempts
                """,
                {"now": now},
            )
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT id
                    FROM jobs
                    WHERE attempt < max_attempts
                      AND (
                          (status = 'queued' AND available_at <= %(now)s)
                          OR (status = 'running' AND lease_expires_at <= %(now)s)
                      )
                    ORDER BY
                        CASE WHEN status = 'running' THEN lease_expires_at ELSE available_at END,
                        created_at,
                        id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE jobs AS job
                SET status = 'running', attempt = job.attempt + 1,
                    lease_owner = %(worker_id)s, lease_expires_at = %(lease_expires_at)s,
                    updated_at = %(now)s
                FROM candidate
                WHERE job.id = candidate.id
                RETURNING job.*
                """,
                {
                    "now": now,
                    "worker_id": worker_id,
                    "lease_expires_at": lease_expires_at,
                },
            )
            row = cursor.fetchone()
        return _job_from_row(row) if row is not None else None

    def heartbeat(self, job_id: UUID, worker_id: str, now: datetime, lease_for: timedelta) -> None:
        self._finish_lease_operation(
            """
            UPDATE jobs SET lease_expires_at = %s, updated_at = %s
            WHERE id = %s AND status = 'running' AND lease_owner = %s
              AND lease_expires_at > %s
            """,
            (_utc(now) + _lease_delta(lease_for), _utc(now), job_id, worker_id, _utc(now)),
            job_id,
        )

    def succeed(self, job_id: UUID, worker_id: str, now: datetime) -> None:
        now = _utc(now)
        self._finish_lease_operation(
            """
            UPDATE jobs
            SET status = 'succeeded', lease_owner = NULL, lease_expires_at = NULL,
                last_error = NULL, updated_at = %s
            WHERE id = %s AND status = 'running' AND lease_owner = %s
              AND lease_expires_at > %s
            """,
            (now, job_id, worker_id, now),
            job_id,
        )

    def fail(
        self,
        job_id: UUID,
        worker_id: str,
        error: Exception,
        now: datetime,
        retry_at: datetime,
    ) -> None:
        now = _utc(now)
        retry_at = _utc(retry_at)
        retryable = isinstance(error, RetryableError)
        with self.database.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs
                SET status = CASE
                        WHEN %s AND attempt < max_attempts THEN 'queued'
                        ELSE 'failed'
                    END,
                    available_at = CASE
                        WHEN %s AND attempt < max_attempts THEN %s
                        ELSE available_at
                    END,
                    lease_owner = NULL, lease_expires_at = NULL,
                    last_error = %s, updated_at = %s
                WHERE id = %s AND status = 'running' AND lease_owner = %s
                  AND lease_expires_at > %s
                """,
                (
                    retryable,
                    retryable,
                    retry_at,
                    type(error).__name__,
                    now,
                    job_id,
                    worker_id,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise LostLeaseError(
                    f"worker {worker_id} does not hold live lease for job {job_id}"
                )

    def _finish_lease_operation(
        self, statement: str, parameters: tuple[object, ...], job_id: UUID
    ) -> None:
        with self.database.connect() as connection, connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            if cursor.rowcount != 1:
                raise LostLeaseError(f"live lease not held for job {job_id}")


def _job_from_row(row: Mapping[str, Any]) -> Job:
    return Job(
        id=row["id"],
        project_id=row["project_id"],
        kind=row["kind"],
        payload=row["payload"],
        status=JobStatus(row["status"]),
        attempt=row["attempt"],
        max_attempts=row["max_attempts"],
        available_at=row["available_at"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


type JobHandler = Callable[[Job], None]


class JobBackend(Protocol):
    def claim(self, worker_id: str, now: datetime, lease_for: timedelta) -> Job | None: ...

    def heartbeat(
        self, job_id: UUID, worker_id: str, now: datetime, lease_for: timedelta
    ) -> None: ...

    def succeed(self, job_id: UUID, worker_id: str, now: datetime) -> None: ...

    def fail(
        self,
        job_id: UUID,
        worker_id: str,
        error: Exception,
        now: datetime,
        retry_at: datetime,
    ) -> None: ...

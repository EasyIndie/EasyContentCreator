from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from time import sleep
from uuid import UUID, uuid4

import pytest
from psycopg.rows import dict_row

from apps.common.database import Database
from apps.worker.main import PollingWorker
from packages.domain import (
    AdapterContractError,
    ContentProject,
    InvalidStateTransition,
    PermanentError,
    ProjectRepository,
    ProjectStatus,
    RetryableError,
)
from packages.pipeline import Job, JobStatus, JobStore, LostLeaseError

NOW = datetime.now(UTC) + timedelta(seconds=1)
LEASE = timedelta(seconds=10)


def create_project(database: Database) -> UUID:
    project_id = uuid4()
    ProjectRepository(database).create(
        ContentProject(project_id, "Job test", ProjectStatus.DRAFT, NOW, NOW)
    )
    return project_id


def read_job(database: Database, job_id: UUID) -> dict[str, object]:
    with (
        database.connect() as connection,
        connection.cursor(row_factory=dict_row) as cursor,
    ):
        cursor.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        row = cursor.fetchone()
    assert row is not None
    return dict(row)


def test_two_connections_claim_a_job_exactly_once(database: Database) -> None:
    store = JobStore(database)
    job_id = store.enqueue("generate", create_project(database), {"seed": 1}, 3, NOW)
    barrier = Barrier(2)

    def claim(worker_id: str) -> Job | None:
        barrier.wait()
        return JobStore(database).claim(worker_id, NOW, LEASE)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(executor.map(claim, ("worker-1", "worker-2")))

    claimed = [job for job in claims if job is not None]
    assert len(claimed) == 1
    assert claimed[0].id == job_id
    assert claimed[0].attempt == 1


def test_expired_lease_is_recovered_by_another_worker(database: Database) -> None:
    store = JobStore(database)
    job_id = store.enqueue("generate", create_project(database), {}, 3, NOW)
    first = store.claim("worker-1", NOW, LEASE)
    assert first is not None

    recovered = store.claim("worker-2", NOW + LEASE, LEASE)

    assert recovered is not None
    assert recovered.id == job_id
    assert recovered.attempt == 2
    assert recovered.lease_owner == "worker-2"


def test_worker_that_lost_lease_cannot_commit_or_heartbeat(database: Database) -> None:
    store = JobStore(database)
    job_id = store.enqueue("generate", create_project(database), {}, 3, NOW)
    assert store.claim("worker-1", NOW, LEASE) is not None
    recovered_at = NOW + LEASE
    assert store.claim("worker-2", recovered_at, LEASE) is not None

    with pytest.raises(LostLeaseError):
        store.heartbeat(job_id, "worker-1", recovered_at, LEASE)
    with pytest.raises(LostLeaseError):
        store.succeed(job_id, "worker-1", recovered_at)
    with pytest.raises(LostLeaseError):
        store.fail(
            job_id,
            "worker-1",
            RetryableError("retry"),
            recovered_at,
            recovered_at + timedelta(seconds=1),
        )

    store.succeed(job_id, "worker-2", recovered_at)
    assert read_job(database, job_id)["status"] == JobStatus.SUCCEEDED.value


def test_retryable_error_requeues_until_attempts_are_exhausted(database: Database) -> None:
    store = JobStore(database)
    job_id = store.enqueue("generate", create_project(database), {}, 2, NOW)
    assert store.claim("worker-1", NOW, LEASE) is not None
    retry_at = NOW + timedelta(seconds=30)
    store.fail(job_id, "worker-1", RetryableError("transient secret"), NOW, retry_at)
    queued = read_job(database, job_id)
    assert queued["status"] == JobStatus.QUEUED.value
    assert queued["last_error"] == "RetryableError"
    assert store.claim("too-early", NOW, LEASE) is None

    assert store.claim("worker-2", retry_at, LEASE) is not None
    store.fail(
        job_id,
        "worker-2",
        RetryableError("still transient"),
        retry_at,
        retry_at + timedelta(seconds=30),
    )
    assert read_job(database, job_id)["status"] == JobStatus.FAILED.value


@pytest.mark.parametrize(
    "error",
    [
        PermanentError("business"),
        AdapterContractError("contract"),
        InvalidStateTransition("state"),
        ValueError("unexpected"),
    ],
)
def test_non_retryable_errors_fail_closed(database: Database, error: Exception) -> None:
    store = JobStore(database)
    job_id = store.enqueue("generate", create_project(database), {}, 3, NOW)
    assert store.claim("worker", NOW, LEASE) is not None

    store.fail(job_id, "worker", error, NOW, NOW + timedelta(seconds=30))

    row = read_job(database, job_id)
    assert row["status"] == JobStatus.FAILED.value
    assert row["last_error"] == type(error).__name__


class CountingStore:
    def __init__(self, delegate: JobStore) -> None:
        self.delegate = delegate
        self.heartbeats = 0

    def claim(self, worker_id: str, now: datetime, lease_for: timedelta) -> Job | None:
        return self.delegate.claim(worker_id, now, lease_for)

    def heartbeat(self, job_id: UUID, worker_id: str, now: datetime, lease_for: timedelta) -> None:
        self.delegate.heartbeat(job_id, worker_id, now, lease_for)
        self.heartbeats += 1

    def succeed(self, job_id: UUID, worker_id: str, now: datetime) -> None:
        self.delegate.succeed(job_id, worker_id, now)

    def fail(
        self,
        job_id: UUID,
        worker_id: str,
        error: Exception,
        now: datetime,
        retry_at: datetime,
    ) -> None:
        self.delegate.fail(job_id, worker_id, error, now, retry_at)


def test_polling_worker_heartbeats_during_handler(database: Database) -> None:
    store = JobStore(database)
    project_id = create_project(database)
    now = datetime.now(UTC)
    job_id = store.enqueue("slow", project_id, {}, 2, now)
    counting = CountingStore(store)
    worker = PollingWorker(
        database,
        poll_interval_seconds=0.01,
        handlers={"slow": lambda _job: sleep(0.18)},
        worker_id="worker-heartbeat",
        lease_for=timedelta(seconds=0.09),
        heartbeat_interval_seconds=0.02,
        job_store=counting,
    )

    worker.poll_once()

    assert counting.heartbeats >= 2
    assert read_job(database, job_id)["status"] == JobStatus.SUCCEEDED.value

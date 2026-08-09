from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from apps.worker.main import PollingWorker
from packages.domain import PermanentError
from packages.pipeline import Job, JobStatus

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


class StubDatabase:
    def __init__(self, ready: bool) -> None:
        self.ready = ready
        self.calls = 0

    def is_ready(self) -> bool:
        self.calls += 1
        return self.ready


class StubJobs:
    def __init__(self, job: Job | None = None) -> None:
        self.job = job
        self.claims = 0
        self.succeeded: list[UUID] = []
        self.failures: list[Exception] = []

    def claim(self, worker_id: str, now: datetime, lease_for: timedelta) -> Job | None:
        self.claims += 1
        claimed, self.job = self.job, None
        return claimed

    def heartbeat(self, job_id: UUID, worker_id: str, now: datetime, lease_for: timedelta) -> None:
        return None

    def succeed(self, job_id: UUID, worker_id: str, now: datetime) -> None:
        self.succeeded.append(job_id)

    def fail(
        self,
        job_id: UUID,
        worker_id: str,
        error: Exception,
        now: datetime,
        retry_at: datetime,
    ) -> None:
        self.failures.append(error)


def make_job(kind: str = "known") -> Job:
    return Job(
        id=uuid4(),
        project_id=uuid4(),
        kind=kind,
        payload={},
        status=JobStatus.RUNNING,
        attempt=1,
        max_attempts=2,
        available_at=NOW,
        lease_owner="worker-test",
        lease_expires_at=NOW + timedelta(minutes=1),
        last_error=None,
        created_at=NOW,
        updated_at=NOW,
    )


def test_poll_checks_database_once_and_claims_at_most_one_job() -> None:
    database = StubDatabase(ready=True)
    jobs = StubJobs()
    worker = PollingWorker(
        database,
        1,
        clock=lambda: NOW,
        job_store=jobs,  # type: ignore[arg-type]
    )

    worker.poll_once()

    assert database.calls == 1
    assert jobs.claims == 1


def test_stopped_worker_does_not_check_or_claim() -> None:
    database = StubDatabase(ready=True)
    jobs = StubJobs(make_job())
    worker = PollingWorker(
        database,
        1,
        clock=lambda: NOW,
        job_store=jobs,  # type: ignore[arg-type]
    )
    worker.stop()

    worker.poll_once()

    assert database.calls == 0
    assert jobs.claims == 0


def test_unknown_job_kind_fails_permanently() -> None:
    database = StubDatabase(ready=True)
    jobs = StubJobs(make_job("unknown"))
    worker = PollingWorker(
        database,
        1,
        worker_id="worker-test",
        clock=lambda: NOW,
        job_store=jobs,  # type: ignore[arg-type]
    )

    worker.poll_once()

    assert len(jobs.failures) == 1
    assert isinstance(jobs.failures[0], PermanentError)

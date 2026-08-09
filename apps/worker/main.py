import logging
import signal
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from apps.common.config import get_settings
from apps.common.database import Database
from packages.domain.errors import AdapterContractError, PermanentError, RetryableError
from packages.pipeline import Job, JobBackend, JobHandler, JobStore, LostLeaseError

LOGGER = logging.getLogger(__name__)


class PollingWorker:
    """Poll one leased Job at a time and dispatch it to a registered handler."""

    def __init__(
        self,
        database: Database,
        poll_interval_seconds: float,
        *,
        handlers: Mapping[str, JobHandler] | None = None,
        worker_id: str | None = None,
        lease_for: timedelta = timedelta(seconds=30),
        heartbeat_interval_seconds: float = 10,
        clock: Callable[[], datetime] | None = None,
        job_store: JobBackend | None = None,
    ) -> None:
        if poll_interval_seconds <= 0 or heartbeat_interval_seconds <= 0:
            raise ValueError("poll and heartbeat intervals must be positive")
        if lease_for <= timedelta(0):
            raise ValueError("lease_for must be positive")
        self._database = database
        self._poll_interval_seconds = poll_interval_seconds
        self._handlers = dict(handlers or {})
        self._worker_id = worker_id or f"worker-{uuid4()}"
        self._lease_for = lease_for
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._jobs = job_store or JobStore(database)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> None:
        if self._stop.is_set():
            return
        if not self._database.is_ready():
            LOGGER.warning("database unavailable; next poll will retry")
            return
        job = self._jobs.claim(self._worker_id, self._clock(), self._lease_for)
        if job is None:
            return
        handler = self._handlers.get(job.kind)
        if handler is None:
            self._fail(job, PermanentError("unknown job kind"))
            return
        self._run_handler(job, handler)

    def _run_handler(self, job: Job, handler: JobHandler) -> None:
        finished = threading.Event()
        heartbeat_error: list[LostLeaseError] = []

        def heartbeat() -> None:
            while not finished.wait(self._heartbeat_interval_seconds):
                try:
                    self._jobs.heartbeat(job.id, self._worker_id, self._clock(), self._lease_for)
                except LostLeaseError as error:
                    heartbeat_error.append(error)
                    return

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        try:
            handler(job)
        except RetryableError as error:
            self._fail(job, error)
        except Exception as error:
            LOGGER.warning(
                "job handler raised",
                extra={**self._log_context(job), "error_class": type(error).__name__},
            )
        else:
            if heartbeat_error:
                LOGGER.warning("job lease lost before completion", extra=self._log_context(job))
            else:
                try:
                    self._jobs.require_terminal(job.id)
                except AdapterContractError as error:
                    self._fail(job, error)
        finally:
            finished.set()
            heartbeat_thread.join()

    def _fail(self, job: Job, error: Exception) -> None:
        now = self._clock()
        retry_at = now + timedelta(seconds=self._poll_interval_seconds)
        try:
            self._jobs.fail(job.id, self._worker_id, error, now, retry_at)
        except LostLeaseError:
            LOGGER.warning("job lease lost before failure", extra=self._log_context(job))
            return
        LOGGER.warning(
            "job handler failed",
            extra={**self._log_context(job), "error_class": type(error).__name__},
        )

    @staticmethod
    def _log_context(job: Job) -> dict[str, object]:
        return {
            "job_id": str(job.id),
            "project_id": str(job.project_id),
            "job_kind": job.kind,
            "attempt": job.attempt,
        }

    def run(self) -> None:
        LOGGER.info("worker started")
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._poll_interval_seconds)
        LOGGER.info("worker stopped")


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    worker = PollingWorker(Database(settings.database_url), settings.worker_poll_interval_seconds)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: worker.stop())
    signal.signal(signal.SIGINT, lambda _signum, _frame: worker.stop())
    worker.run()


if __name__ == "__main__":
    main()

import logging
import signal
import threading

from apps.common.config import get_settings
from apps.common.database import Database

LOGGER = logging.getLogger(__name__)


class PollingWorker:
    """Lifecycle skeleton; job claiming is intentionally deferred to M1."""

    def __init__(self, database: Database, poll_interval_seconds: float) -> None:
        self._database = database
        self._poll_interval_seconds = poll_interval_seconds
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> None:
        if not self._database.is_ready():
            LOGGER.warning("database unavailable; next poll will retry")
            return
        LOGGER.debug("database ready; no job handlers registered")

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

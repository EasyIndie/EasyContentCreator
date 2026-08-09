from apps.worker.main import PollingWorker


class StubDatabase:
    def __init__(self, ready: bool) -> None:
        self.ready = ready
        self.calls = 0

    def is_ready(self) -> bool:
        self.calls += 1
        return self.ready


def test_poll_checks_database_once() -> None:
    database = StubDatabase(ready=True)
    worker = PollingWorker(database, poll_interval_seconds=1)  # type: ignore[arg-type]

    worker.poll_once()

    assert database.calls == 1

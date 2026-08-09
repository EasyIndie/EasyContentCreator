from fastapi.testclient import TestClient

from apps.api.main import app, get_database
from apps.common.database import Database


class ReadyDatabase(Database):
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    def is_ready(self) -> bool:
        return self.ready


def test_liveness_does_not_require_database() -> None:
    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["database"] == "not_checked"


def test_readiness_reports_available_database() -> None:
    app.dependency_overrides[get_database] = lambda: ReadyDatabase(True)
    try:
        response = TestClient(app).get("/health/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["database"] == "ok"


def test_readiness_fails_closed_when_database_is_unavailable() -> None:
    app.dependency_overrides[get_database] = lambda: ReadyDatabase(False)
    try:
        response = TestClient(app).get("/health/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"

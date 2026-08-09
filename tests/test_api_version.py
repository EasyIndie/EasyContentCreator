from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from apps.api.main import app
from apps.common.config import Settings, get_settings


def test_version_uses_development_defaults() -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    try:
        response = TestClient(app).get("/version")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"version": "dev", "commit": "unknown"}


def test_version_uses_build_environment(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("ECC_VERSION", "1.2.3")
    monkeypatch.setenv("ECC_COMMIT", "abc1234")
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    try:
        response = TestClient(app).get("/version")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"version": "1.2.3", "commit": "abc1234"}

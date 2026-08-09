import json
import os
import subprocess
from pathlib import Path


def test_compose_injects_prefixed_database_and_persistent_artifact_root() -> None:
    repository_root = Path(__file__).parents[2]
    environment = {
        **os.environ,
        "POSTGRES_PASSWORD": "compose-validation-only",
    }
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=repository_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    config = json.loads(result.stdout)
    services = config["services"]
    expected_database_url = "postgresql://ecc:compose-validation-only@postgres:5432/ecc"

    assert services["api"]["environment"]["ECC_DATABASE_URL"] == expected_database_url
    assert "DATABASE_URL" not in services["api"]["environment"]
    assert services["worker"]["environment"]["ECC_DATABASE_URL"] == expected_database_url
    assert "DATABASE_URL" not in services["worker"]["environment"]
    assert services["worker"]["environment"]["ECC_ARTIFACT_ROOT"] == "/data/artifacts"
    assert {
        "type": "volume",
        "source": "artifacts",
        "target": "/data/artifacts",
        "volume": {},
    } in services["worker"]["volumes"]

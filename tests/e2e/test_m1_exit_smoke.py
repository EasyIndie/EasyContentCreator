from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

REPOSITORY_ROOT = Path(__file__).parents[2]
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"
PASSWORD = "ecc034-validation-only"
SOURCE_ID = UUID("00000000-0000-4000-8000-000000000034")


def _compose(
    project: str,
    environment: dict[str, str],
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            project,
            "--file",
            str(COMPOSE_FILE),
            *arguments,
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=check,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _published_port(project: str, environment: dict[str, str], service: str, port: int) -> int:
    output = _compose(project, environment, "port", service, str(port)).stdout.strip()
    return int(output.rsplit(":", 1)[1])


def _request(
    base_url: str,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    payload = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=payload,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def _wait_for_job(base_url: str, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        _status, job = _request(base_url, "GET", f"/jobs/{job_id}")
        if job["status"] in {"succeeded", "failed"}:
            return cast(dict[str, object], job)
        time.sleep(0.5)
    raise AssertionError(f"job did not reach terminal state: {job_id}")


@pytest.mark.compose
def test_m1_fake_fact_card_compose_exit_smoke() -> None:
    suffix = uuid4().hex[:10]
    project = f"ecc034-smoke-{suffix}"
    environment = {
        **os.environ,
        "API_PORT": "0",
        "WEB_PORT": "0",
        "ECC_IMAGE": f"ecc034-app-{suffix}:dev",
        "ECC_WEB_IMAGE": f"ecc034-web-{suffix}:dev",
        "POSTGRES_PASSWORD": PASSWORD,
    }
    try:
        _compose(project, environment, "build", "api", "web")
        _compose(project, environment, "up", "--detach", "--wait", "--wait-timeout", "180")
        api_url = f"http://127.0.0.1:{_published_port(project, environment, 'api', 8000)}"
        web_port = _published_port(project, environment, "web", 80)
        assert web_port > 0

        seed = (
            "from datetime import UTC,datetime; from uuid import UUID; "
            "from apps.common.config import get_settings; "
            "from apps.common.database import Database; "
            "from packages.domain import Source,SourceExcerpt,SourceKind,SourceRepository; "
            "source=Source(UUID('00000000-0000-4000-8000-000000000034'),"
            "SourceKind.OFFICIAL_DOCUMENTATION,'M1 audit source','https://example.test/audit',"
            "datetime.now(UTC),'a'*64,'Audited official summary',"
            "(SourceExcerpt(UUID('00000000-0000-4000-8000-000000000035'),"
            "'Verified M1 evidence','section-1'),)); "
            "SourceRepository(Database(get_settings().database_url)).add(source)"
        )
        _compose(project, environment, "exec", "-T", "api", "python", "-c", seed)

        created_status, created = _request(
            api_url, "POST", "/projects", {"title": "M1 Compose exit audit"}
        )
        assert created_status == 201
        project_id = created["id"]
        generation_body = {
            "source_ids": [str(SOURCE_ID)],
            "template_version": "fact-card-v1",
            "budget_units": 1000,
        }
        first_status, first = _request(
            api_url,
            "POST",
            f"/projects/{project_id}/generate",
            generation_body,
            {"Idempotency-Key": "m1-audit-v1"},
        )
        assert first_status == 202
        first_job = _wait_for_job(api_url, first["job_id"])
        assert first_job["status"] == "succeeded"
        assert {"payload", "lease_owner", "lease_expires_at"}.isdisjoint(first_job)

        _status, generated = _request(api_url, "GET", f"/projects/{project_id}")
        assert generated["status"] == "review_required"
        first_ref = generated["current_artifacts"]["fact_card"]
        _status, artifact = _request(
            api_url,
            "GET",
            f"/artifacts/{first_ref['artifact_id']}/versions/1?project_id={project_id}",
        )
        assert artifact["version"] == 1
        assert artifact["citations"][0]["source_id"] == str(SOURCE_ID)
        assert "storage_path" not in json.dumps(artifact)
        _status, source = _request(api_url, "GET", f"/sources/{SOURCE_ID}")
        assert source["sha256"] == "a" * 64
        assert source["excerpts"][0]["text"] == "Verified M1 evidence"

        rejected_status, _review = _request(
            api_url,
            "POST",
            f"/projects/{project_id}/reviews",
            {
                "decision": "reject",
                "note": "Regenerate framing",
                "expected_revision": generated["revision"],
            },
        )
        assert rejected_status == 201
        _status, rejected = _request(api_url, "GET", f"/projects/{project_id}")
        assert rejected["status"] == "failed"
        assert rejected["current_artifacts"]["fact_card"] == first_ref

        _status, old_replay = _request(
            api_url,
            "POST",
            f"/projects/{project_id}/generate",
            generation_body,
            {"Idempotency-Key": "m1-audit-v1"},
        )
        assert old_replay["job_id"] == first["job_id"]
        _status, still_rejected = _request(api_url, "GET", f"/projects/{project_id}")
        assert still_rejected["status"] == "failed"

        _status, second = _request(
            api_url,
            "POST",
            f"/projects/{project_id}/generate",
            generation_body,
            {"Idempotency-Key": "m1-audit-v2"},
        )
        assert _wait_for_job(api_url, second["job_id"])["status"] == "succeeded"
        _status, regenerated = _request(api_url, "GET", f"/projects/{project_id}")
        assert regenerated["status"] == "review_required"
        assert regenerated["current_artifacts"]["fact_card"]["version"] == 2

        approved_status, _approved = _request(
            api_url,
            "POST",
            f"/projects/{project_id}/reviews",
            {
                "decision": "approve",
                "note": "Evidence approved",
                "expected_revision": regenerated["revision"],
            },
        )
        assert approved_status == 201
        _status, approved = _request(api_url, "GET", f"/projects/{project_id}")
        assert approved["status"] == "approved"
        _status, jobs = _request(api_url, "GET", f"/projects/{project_id}/jobs")
        assert [item["status"] for item in jobs["items"]] == ["succeeded", "succeeded"]
        assert "payload" not in json.dumps(jobs)

        with urllib.request.urlopen(f"http://localhost:{web_port}/", timeout=10) as response:
            web_html = response.read().decode()
            assert response.status == 200
        assert '<div id="root"></div>' in web_html
    finally:
        cleanup = _compose(
            project,
            environment,
            "down",
            "--volumes",
            "--remove-orphans",
            "--rmi",
            "local",
            check=False,
        )
        assert cleanup.returncode == 0

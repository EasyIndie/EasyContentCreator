from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from websockets.sync.client import ClientConnection, connect

REPOSITORY_ROOT = Path(__file__).parents[2]
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"
PASSWORD = "ecc034-validation-only"
SOURCE_ID = UUID("00000000-0000-4000-8000-000000000034")


class BrowserPage:
    def __init__(self, connection: ClientConnection) -> None:
        self._connection = connection
        self._message_id = 0

    def command(self, method: str, parameters: dict[str, object] | None = None) -> dict[str, Any]:
        self._message_id += 1
        message_id = self._message_id
        self._connection.send(
            json.dumps({"id": message_id, "method": method, "params": parameters or {}})
        )
        while True:
            response = json.loads(self._connection.recv())
            if response.get("id") == message_id:
                if "error" in response:
                    raise AssertionError(f"browser command failed: {response['error']}")
                return cast(dict[str, Any], response["result"])

    def evaluate(self, expression: str) -> Any:
        result = self.command(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
        )
        remote = result["result"]
        if "exceptionDetails" in result:
            raise AssertionError(f"browser expression failed: {result['exceptionDetails']}")
        return remote.get("value")

    def wait(self, expression: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.evaluate(expression):
                return
            time.sleep(0.2)
        body = self.evaluate("document.body.innerText")
        raise AssertionError(f"browser condition timed out: {expression}\nDOM text:\n{body}")


def _chrome_executable() -> str:
    configured = os.environ.get("CHROME_BIN")
    candidates = (
        configured,
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise AssertionError("system Chrome/Chromium is required for M1 browser acceptance")


def _free_local_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return cast(int, listener.getsockname()[1])


@contextmanager
def _browser_page(url: str) -> Iterator[BrowserPage]:
    debugging_port = _free_local_port()
    with tempfile.TemporaryDirectory(prefix="ecc034-chrome-") as profile:
        process = subprocess.Popen(
            [
                _chrome_executable(),
                "--headless=new",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
                "--no-first-run",
                "--no-sandbox",
                f"--remote-debugging-port={debugging_port}",
                f"--user-data-dir={profile}",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            targets: list[dict[str, Any]] = []
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{debugging_port}/json/list", timeout=1
                    ) as response:
                        targets = json.loads(response.read())
                    if targets:
                        break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.1)
            page_target = next((item for item in targets if item.get("type") == "page"), None)
            if page_target is None:
                raise AssertionError("Chrome did not expose a page target")
            with connect(page_target["webSocketDebuggerUrl"], open_timeout=5) as connection:
                page = BrowserPage(connection)
                page.command("Runtime.enable")
                page.wait("document.readyState === 'complete'")
                yield page
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _set_control(page: BrowserPage, selector: str, value: str) -> None:
    page.evaluate(
        "(() => {"
        f"const element = document.querySelector({json.dumps(selector)});"
        """
          if (!element) return false;
          const prototype = element instanceof HTMLTextAreaElement
            ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        """
        "Object.getOwnPropertyDescriptor(prototype, 'value').set.call("
        f"element, {json.dumps(value)});"
        """
          element.dispatchEvent(new Event('input', {bubbles: true}));
          return true;
        })()"""
    )


def _click_text(page: BrowserPage, selector: str, text: str) -> None:
    clicked = page.evaluate(
        "(() => {"
        f"const element = [...document.querySelectorAll({json.dumps(selector)})]"
        f".find(item => item.textContent.includes({json.dumps(text)}));"
        """
          if (!element) return false;
          element.click();
          return true;
        })()"""
    )
    assert clicked is True


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
        assert artifact["upstream"] == []
        assert artifact["metadata"] == {
            "capability": "fact_card",
            "template_version": "fact-card-v1",
            "parameters": {"source_ids": [str(SOURCE_ID)]},
        }
        assert "storage_path" not in json.dumps(artifact)
        _status, source = _request(api_url, "GET", f"/sources/{SOURCE_ID}")
        assert source["sha256"] == "a" * 64
        assert source["excerpts"][0]["text"] == "Verified M1 evidence"

        for web_host in ("localhost", "127.0.0.1"):
            with urllib.request.urlopen(f"http://{web_host}:{web_port}/", timeout=10) as response:
                assert response.status == 200
                assert '<div id="root"></div>' in response.read().decode()

        with _browser_page(f"http://localhost:{web_port}/") as page:
            page.wait("document.body.innerText.includes('M1 Compose exit audit')")
            _click_text(page, "button", "M1 Compose exit audit")
            page.wait("document.body.innerText.includes('证据链已加载，共 1 条引用。')")
            browser_text = page.evaluate("document.body.innerText")
            for expected in (
                "M1 audit source",
                "Verified M1 evidence",
                "generate_fact_card · succeeded",
                "Artifact SHA",
                "fact-card-v1",
            ):
                assert expected in browser_text
            resource_urls = page.evaluate(
                "performance.getEntriesByType('resource').map(entry => entry.name)"
            )
            assert any("/api/projects" in item for item in resource_urls)
            assert any("/api/sources/" in item for item in resource_urls)
            assert all(
                f"localhost:{web_port}/api/" in item for item in resource_urls if "/api/" in item
            )

            assert page.evaluate("document.querySelector('input[value=reject]').click()") is None
            _set_control(page, "#review-note", "Regenerate framing")
            _click_text(page, "button", "提交审核")
            page.wait("document.body.innerText.includes('审核已驳回。')")
            page.wait("document.body.innerText.includes('failed')")

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

            page.wait("document.querySelector('input[id^=idempotency-key-]') !== null")
            _set_control(page, "input[id^=idempotency-key-]", "m1-audit-v2")
            _set_control(page, "textarea[id^=source-ids-]", str(SOURCE_ID))
            _click_text(page, "button", "提交恢复")
            page.wait("document.body.innerText.includes('恢复任务已提交：')")
            _status, jobs_after_submit = _request(api_url, "GET", f"/projects/{project_id}/jobs")
            second_job_id = jobs_after_submit["items"][0]["id"]
            assert second_job_id != first["job_id"]
            assert _wait_for_job(api_url, second_job_id)["status"] == "succeeded"

            _click_text(page, "button", "M1 Compose exit audit")
            page.wait("document.body.innerText.includes('fact_card · v2')")
            page.wait("document.body.innerText.includes('证据链已加载，共 1 条引用。')")
            assert page.evaluate("document.querySelector('input[value=approve]').click()") is None
            _set_control(page, "#review-note", "Evidence approved")
            _click_text(page, "button", "提交审核")
            page.wait("document.body.innerText.includes('审核已批准。')")
            page.wait("document.body.innerText.includes('approved')")

        _status, approved = _request(api_url, "GET", f"/projects/{project_id}")
        assert approved["status"] == "approved"
        assert approved["current_artifacts"]["fact_card"]["version"] == 2
        _status, jobs = _request(api_url, "GET", f"/projects/{project_id}/jobs")
        assert [item["status"] for item in jobs["items"]] == ["succeeded", "succeeded"]
        assert "payload" not in json.dumps(jobs)
        byte_audit = (
            "from hashlib import sha256; from apps.common.config import get_settings; "
            "from apps.common.database import Database; "
            "from packages.domain import ArtifactRepository; "
            f"from uuid import UUID; artifact_id=UUID('{first_ref['artifact_id']}'); "
            "repository=ArtifactRepository(Database(get_settings().database_url)); "
            "artifacts=[repository.get(artifact_id, version) for version in (1,2)]; "
            "root=get_settings().artifact_root; "
            "contents=[(root / item.storage_path).read_bytes() for item in artifacts]; "
            "assert all(sha256(data).hexdigest() == item.ref.sha256 "
            "for data,item in zip(contents,artifacts,strict=True)); "
            "assert artifacts[0].ref.version == 1 and artifacts[1].ref.version == 2; "
            "assert artifacts[0].citations == artifacts[1].citations; print('bytes-lineage-ok')"
        )
        audited = _compose(project, environment, "exec", "-T", "worker", "python", "-c", byte_audit)
        assert audited.stdout.strip() == "bytes-lineage-ok"
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
        if cleanup.returncode != 0 and sys.exc_info()[0] is None:
            raise AssertionError(f"Compose cleanup failed: {cleanup.stderr}")

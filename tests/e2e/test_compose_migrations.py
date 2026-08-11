from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

REPOSITORY_ROOT = Path(__file__).parents[2]
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"
MIGRATIONS_DIRECTORY = REPOSITORY_ROOT / "migrations"
PASSWORD = "ecc033-validation-only"


def _environment(image_suffix: str) -> dict[str, str]:
    return {
        **os.environ,
        "API_PORT": "0",
        "WEB_PORT": "0",
        "ECC_IMAGE": f"ecc033-app-{image_suffix}:dev",
        "ECC_WEB_IMAGE": f"ecc033-web-{image_suffix}:dev",
        "POSTGRES_PASSWORD": PASSWORD,
    }


def _compose(
    project: str,
    environment: dict[str, str],
    *arguments: str,
    override: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--file",
        str(COMPOSE_FILE),
    ]
    if override is not None:
        command.extend(("--file", str(override)))
    command.extend(arguments)
    return subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=check,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _migration_names(project: str, environment: dict[str, str]) -> tuple[str, ...]:
    result = _compose(
        project,
        environment,
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "ecc",
        "-d",
        "ecc",
        "-Atc",
        "SELECT name FROM schema_migrations ORDER BY name",
    )
    return tuple(line for line in result.stdout.splitlines() if line)


def _cleanup(project: str, environment: dict[str, str], *, remove_images: bool) -> None:
    arguments = ["down", "--volumes", "--remove-orphans"]
    if remove_images:
        arguments.extend(("--rmi", "local"))
    _compose(project, environment, *arguments)


def _cleanup_projects(
    projects: tuple[tuple[str, bool], ...], environment: dict[str, str]
) -> tuple[str, ...]:
    errors = []
    for project, remove_images in projects:
        try:
            _cleanup(project, environment, remove_images=remove_images)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            errors.append(f"{project}: {error}")
    return tuple(errors)


@pytest.mark.compose
def test_compose_migrations_are_idempotent_and_fail_closed() -> None:
    suffix = uuid4().hex[:10]
    success_project = f"ecc033-success-{suffix}"
    failure_project = f"ecc033-failure-{suffix}"
    unavailable_project = f"ecc033-unavailable-{suffix}"
    environment = _environment(suffix)

    with tempfile.TemporaryDirectory(prefix="ecc033-") as temporary_directory:
        bad_migration = Path(temporary_directory) / "999_bad.sql"
        bad_migration.write_text("THIS IS NOT VALID SQL;\n", encoding="utf-8")
        override = Path(temporary_directory) / "compose.bad.yaml"
        override.write_text(
            "services:\n"
            "  migrate:\n"
            "    volumes:\n"
            f"      - {bad_migration}:/app/migrations/999_bad.sql:ro\n",
            encoding="utf-8",
        )

        try:
            _compose(success_project, environment, "build", "api", "web")
            _compose(
                success_project,
                environment,
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "180",
            )

            expected = tuple(
                path.name for path in sorted(MIGRATIONS_DIRECTORY.glob("[0-9][0-9][0-9]_*.sql"))
            )
            assert _migration_names(success_project, environment) == expected
            _compose(
                success_project,
                environment,
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "import urllib.request; "
                'assert b\'\\"database\\":\\"ok\\"\' in '
                "urllib.request.urlopen('http://127.0.0.1:8000/health/ready').read()",
            )
            running = set(
                _compose(
                    success_project,
                    environment,
                    "ps",
                    "--status",
                    "running",
                    "--services",
                ).stdout.splitlines()
            )
            assert {"postgres", "api", "worker", "web"} <= running

            _compose(success_project, environment, "run", "--rm", "migrate")
            assert _migration_names(success_project, environment) == expected

            _compose(
                failure_project,
                environment,
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "120",
                "postgres",
                override=override,
            )
            failed = _compose(
                failure_project,
                environment,
                "up",
                "--detach",
                "api",
                "worker",
                override=override,
                check=False,
            )
            assert failed.returncode != 0
            assert PASSWORD not in failed.stdout
            assert PASSWORD not in failed.stderr
            migrate_id = _compose(
                failure_project,
                environment,
                "ps",
                "--all",
                "--quiet",
                "migrate",
                override=override,
            ).stdout.strip()
            assert migrate_id
            inspected = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.ExitCode}}", migrate_id],
                check=True,
                capture_output=True,
                text=True,
            )
            assert int(inspected.stdout.strip()) != 0
            migrate_logs = _compose(
                failure_project, environment, "logs", "--no-color", "migrate", override=override
            )
            assert PASSWORD not in migrate_logs.stdout
            assert PASSWORD not in migrate_logs.stderr
            failure_running = set(
                _compose(
                    failure_project,
                    environment,
                    "ps",
                    "--status",
                    "running",
                    "--services",
                    override=override,
                ).stdout.splitlines()
            )
            assert "api" not in failure_running
            assert "worker" not in failure_running

            unavailable = _compose(
                unavailable_project,
                environment,
                "run",
                "--rm",
                "--no-deps",
                "--environment",
                "ECC_DATABASE_URL=postgresql://ecc:ecc033-unavailable-secret@127.0.0.1:1/ecc",
                "migrate",
                check=False,
            )
            assert unavailable.returncode != 0
            assert "ecc033-unavailable-secret" not in unavailable.stdout
            assert "ecc033-unavailable-secret" not in unavailable.stderr
            unavailable_running = _compose(
                unavailable_project, environment, "ps", "--status", "running", "--services"
            ).stdout.splitlines()
            assert "api" not in unavailable_running
            assert "worker" not in unavailable_running
        finally:
            cleanup_errors = _cleanup_projects(
                (
                    (failure_project, False),
                    (unavailable_project, False),
                    (success_project, True),
                ),
                environment,
            )
            if cleanup_errors and sys.exc_info()[0] is None:
                raise AssertionError("; ".join(cleanup_errors))

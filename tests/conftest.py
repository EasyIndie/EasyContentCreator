import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest
from psycopg import sql

from apps.common.database import Database
from migrations import run_migrations

MIGRATIONS_DIR = Path(__file__).parents[1] / "migrations"


def _schema_url(database_url: str, schema: str) -> str:
    parts = urlsplit(database_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[tuple[Database, tuple[str, ...]]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = f"ecc_test_{uuid4().hex}"
    admin = Database(database_url)
    with admin.connect() as connection, connection.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    database = Database(_schema_url(database_url, schema))
    try:
        applied = run_migrations(database)
        yield database, applied
    finally:
        if not schema.startswith("ecc_test_"):
            raise RuntimeError("refusing to remove an unrecognized test schema")
        with admin.connect() as connection, connection.cursor() as cursor:
            cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def database(migrated_database: tuple[Database, tuple[str, ...]]) -> Iterator[Database]:
    database, _ = migrated_database
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            TRUNCATE generation_requests, reviews, jobs,
                     project_current_artifacts, artifact_citations,
                     artifact_upstream, artifacts, source_excerpts, sources, projects
            RESTART IDENTITY CASCADE
            """
        )
    yield database


@pytest.fixture
def legacy_database() -> Iterator[Database]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = f"ecc_test_{uuid4().hex}"
    admin = Database(database_url)
    with admin.connect() as connection, connection.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    database = Database(_schema_url(database_url, schema))
    try:
        with database.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE schema_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute((MIGRATIONS_DIR / "001_initial.sql").read_text(encoding="utf-8"))
            cursor.execute("INSERT INTO schema_migrations (name) VALUES ('001_initial.sql')")
        yield database
    finally:
        with admin.connect() as connection, connection.cursor() as cursor:
            cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

from pathlib import Path

from apps.common.database import Database

MIGRATIONS_DIR = Path(__file__).parent


def run_migrations(database: Database, directory: Path = MIGRATIONS_DIR) -> tuple[str, ...]:
    """Apply pending numbered SQL files in one transaction and return applied names."""
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    applied: list[str] = []
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute("LOCK TABLE schema_migrations IN EXCLUSIVE MODE")
        cursor.execute("SELECT name FROM schema_migrations")
        existing = {row[0] for row in cursor.fetchall()}
        for path in files:
            if path.name in existing:
                continue
            cursor.execute(path.read_text(encoding="utf-8"))
            cursor.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return tuple(applied)

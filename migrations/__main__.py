import logging

from apps.common.config import get_settings
from apps.common.database import Database
from migrations import run_migrations


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    applied = run_migrations(Database(get_settings().database_url))
    if applied:
        logging.info("applied migrations: %s", ", ".join(applied))
    else:
        logging.info("database schema is current")


if __name__ == "__main__":
    main()

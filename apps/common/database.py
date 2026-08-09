from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import Connection


class Database:
    """Small connection boundary shared by API probes and the polling worker."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    @contextmanager
    def connect(self) -> Iterator[Connection[Any]]:
        with psycopg.connect(self._database_url, connect_timeout=3) as connection:
            yield connection

    def is_ready(self) -> bool:
        try:
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                return cursor.fetchone() == (1,)
        except psycopg.Error:
            return False

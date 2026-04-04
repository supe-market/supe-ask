from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterable

import psycopg2
from psycopg2.extras import RealDictCursor

from .config import settings


class Database:
    def __init__(self) -> None:
        ssl_mode = "require" if settings.db_ssl else "disable"
        self._dsn = (
            f"host={settings.db_host} "
            f"port={settings.db_port} "
            f"dbname={settings.db_name} "
            f"user={settings.db_user} "
            f"password={settings.db_password} "
            f"sslmode={ssl_mode}"
        )

    @contextmanager
    def connection(self):
        connection = psycopg2.connect(self._dsn)
        try:
            yield connection
        finally:
            connection.close()

    def fetch_one(self, query: str, params: Iterable[Any] | None = None) -> dict[str, Any] | None:
        with self.connection() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(query, params or [])
                row = cursor.fetchone()
                return dict(row) if row else None

    def fetch_all(self, query: str, params: Iterable[Any] | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(query, params or [])
                rows = cursor.fetchall()
                return [dict(row) for row in rows]

    def execute(self, query: str, params: Iterable[Any] | None = None) -> None:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params or [])
            connection.commit()

    def execute_returning(self, query: str, params: Iterable[Any] | None = None) -> dict[str, Any] | None:
        with self.connection() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(query, params or [])
                row = cursor.fetchone()
            connection.commit()
            return dict(row) if row else None


db = Database()


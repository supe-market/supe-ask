from __future__ import annotations

from pathlib import Path

from .db import db


def run_migrations() -> None:
    db.execute(
        """
        create table if not exists ask_service_migrations (
          migration_name text primary key,
          applied_at timestamptz not null default now()
        )
        """
    )

    migration_dir = Path(__file__).resolve().parent / "migrations"
    for migration_path in sorted(migration_dir.glob("*.sql")):
        existing = db.fetch_one(
            "select migration_name from ask_service_migrations where migration_name = %s",
            [migration_path.name],
        )
        if existing:
            continue
        sql = migration_path.read_text()
        db.execute(sql)
        db.execute(
            "insert into ask_service_migrations (migration_name) values (%s)",
            [migration_path.name],
        )

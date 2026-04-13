from __future__ import annotations

from collections.abc import MutableMapping

from ..config import settings


def apply_runtime_db_env(env: MutableMapping[str, str]) -> None:
    """Copy the resolved Ask database settings into the runtime subprocess env."""
    env["SUPE_ASK_DB_HOST"] = settings.db_host
    env["SUPE_ASK_DB_PORT"] = str(settings.db_port)
    env["SUPE_ASK_DB_NAME"] = settings.db_name
    env["SUPE_ASK_DB_USER"] = settings.db_user
    env["SUPE_ASK_DB_PASSWORD"] = settings.db_password
    env["SUPE_ASK_DB_SSL"] = "true" if settings.db_ssl else "false"

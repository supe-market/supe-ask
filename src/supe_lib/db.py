from __future__ import annotations

import os
import re
from typing import Any

import pandas as pd
import psycopg2

from .dataframes import normalize_frame_nulls


READ_ONLY_PATTERN = re.compile(r"^\s*(with|select|explain)\b", re.IGNORECASE | re.DOTALL)
BLOCKED_PATTERN = re.compile(r"\b(insert|update|delete|drop|alter|truncate|copy|grant|revoke|create)\b", re.IGNORECASE)
TENANT_FILTER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_\.]*$")
TENANT_FILTER_TOKEN = "{{tenant_filter}}"


def _dsn() -> str:
    ssl_mode = "require" if os.getenv("SUPE_ASK_DB_SSL", "false").lower() == "true" else "disable"
    return (
        f"host={os.getenv('SUPE_ASK_DB_HOST', 'localhost')} "
        f"port={os.getenv('SUPE_ASK_DB_PORT', '5432')} "
        f"dbname={os.getenv('SUPE_ASK_DB_NAME', 'supe_analytics')} "
        f"user={os.getenv('SUPE_ASK_DB_USER', 'postgres')} "
        f"password={os.getenv('SUPE_ASK_DB_PASSWORD', 'postgres')} "
        f"sslmode={ssl_mode}"
    )


def _normalize_params(params: list | tuple | dict | None) -> list | dict:
    if params is None:
        return []
    if isinstance(params, dict):
        return dict(params)
    if isinstance(params, tuple):
        return list(params)
    if isinstance(params, list):
        return list(params)
    raise TypeError("params must be a list, tuple, dict, or None")


def _validate_statement(statement: str, tenant_id_column: str) -> None:
    if not READ_ONLY_PATTERN.search(statement) or BLOCKED_PATTERN.search(statement) or ";" in statement.rstrip(";"):
        raise ValueError("Only a single read-only SQL statement is allowed")
    if TENANT_FILTER_TOKEN not in statement:
        raise ValueError("SQL must include the {{tenant_filter}} placeholder")
    if not TENANT_FILTER_PATTERN.match(tenant_id_column):
        raise ValueError("tenant_id_column contains invalid characters")


def _tenant_id() -> str:
    tenant_id = os.getenv("SUPE_ASK_TENANT_ID", "").strip()
    if not tenant_id:
        raise ValueError("Tenant context is not available for this execution")
    return tenant_id


def _bind_query(
    sql: str,
    params: list | tuple | dict | None = None,
    tenant_id_column: str = "tenant_id",
) -> tuple[str, list | dict]:
    statement = sql.strip()
    _validate_statement(statement, tenant_id_column)
    tenant_id = _tenant_id()
    query_params = _normalize_params(params)

    if isinstance(query_params, dict):
        statement = statement.replace(TENANT_FILTER_TOKEN, f"{tenant_id_column} = %(tenant_id)s")
        query_params["tenant_id"] = tenant_id
    else:
        statement = statement.replace(TENANT_FILTER_TOKEN, f"{tenant_id_column} = %s")
        query_params.append(tenant_id)

    return statement, query_params


def query_df(
    sql: str,
    params: list | tuple | dict | None = None,
    tenant_id_column: str = "tenant_id",
) -> pd.DataFrame:
    statement, query_params = _bind_query(sql, params=params, tenant_id_column=tenant_id_column)
    connection = psycopg2.connect(_dsn())
    try:
        connection.set_session(readonly=True, autocommit=True)
        return pd.read_sql_query(statement, connection, params=query_params)
    finally:
        connection.close()


def query_records(
    sql: str,
    params: list | tuple | dict | None = None,
    tenant_id_column: str = "tenant_id",
) -> list[dict[str, Any]]:
    frame = query_df(sql, params=params, tenant_id_column=tenant_id_column)
    return normalize_frame_nulls(frame).to_dict(orient="records")


def query_scalar(
    sql: str,
    params: list | tuple | dict | None = None,
    tenant_id_column: str = "tenant_id",
    default: Any = None,
) -> Any:
    frame = query_df(sql, params=params, tenant_id_column=tenant_id_column)
    if frame.empty or frame.shape[1] == 0:
        return default
    value = frame.iloc[0, 0]
    normalized = normalize_frame_nulls(pd.DataFrame({"value": [value]})).iloc[0, 0]
    return default if normalized is None else normalized

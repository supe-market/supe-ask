from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

import pandas as pd
import psycopg2

from .dataframes import normalize_frame_nulls


READ_ONLY_PATTERN = re.compile(r"^\s*(with|select|explain)\b", re.IGNORECASE | re.DOTALL)
BLOCKED_PATTERN = re.compile(r"\b(insert|update|delete|drop|alter|truncate|copy|grant|revoke|create)\b", re.IGNORECASE)
TENANT_FILTER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_\.]*$")
TENANT_FILTER_TOKEN = "{{tenant_filter}}"
# Matches GROUP BY / ORDER BY / HAVING / LIMIT / OFFSET at the outermost query level
_CLAUSE_PATTERN = re.compile(r"\b(GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|OFFSET)\b", re.IGNORECASE)


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


def _validate_statement(statement: str, tenant_id_column: str | None) -> None:
    if not READ_ONLY_PATTERN.search(statement) or BLOCKED_PATTERN.search(statement) or ";" in statement.rstrip(";"):
        raise ValueError("Only a single read-only SQL statement is allowed")
    if tenant_id_column is not None and not TENANT_FILTER_PATTERN.match(tenant_id_column):
        raise ValueError("tenant_id_column contains invalid characters")


def _outermost_where_positions(statement: str) -> list[int]:
    """Return character positions of all WHERE keywords at parenthesis depth 0."""
    positions = []
    depth = 0
    i = 0
    while i < len(statement):
        c = statement[i]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        elif depth == 0 and statement[i:i+5].upper() == 'WHERE':
            before = statement[i - 1] if i > 0 else ' '
            after = statement[i + 5] if i + 5 < len(statement) else ' '
            if not (before.isalnum() or before == '_') and not (after.isalnum() or after == '_'):
                positions.append(i)
        i += 1
    return positions


def _inject_tenant_filter(statement: str, tenant_id_column: str, use_dict: bool) -> str:
    """
    Inject a tenant_id condition into SQL that has no {tenant_filter} placeholder.

    Finds the last outermost WHERE clause and prepends the condition.
    Falls back to adding a WHERE before GROUP BY / ORDER BY / HAVING / LIMIT,
    or appending at the end if none of those exist.
    """
    placeholder = "%(tenant_id)s" if use_dict else "%s"
    condition = f"{tenant_id_column} = {placeholder}"

    where_positions = _outermost_where_positions(statement)
    if where_positions:
        # Inject right after the last top-level WHERE keyword
        pos = where_positions[-1] + len("WHERE")
        rest = statement[pos:].lstrip()
        return statement[:pos] + " " + condition + " AND " + rest

    # No WHERE — add one before the first outermost trailing clause
    depth = 0
    for m in _CLAUSE_PATTERN.finditer(statement):
        depth = sum(1 if c == '(' else -1 if c == ')' else 0 for c in statement[:m.start()])
        if depth == 0:
            return statement[:m.start()] + f"WHERE {condition} " + statement[m.start():]

    # No trailing clause either — append
    return statement + f" WHERE {condition}"


def _tenant_id() -> str:
    tenant_id = os.getenv("SUPE_ASK_TENANT_ID", "").strip()
    if not tenant_id:
        raise ValueError("Tenant context is not available for this execution")
    return tenant_id


def _bind_query(
    sql: str,
    params: list | tuple | dict | None = None,
    tenant_id_column: str | None = "tenant_id",
) -> tuple[str, list | dict]:
    statement = sql.strip()
    _validate_statement(statement, tenant_id_column)
    query_params = _normalize_params(params)

    if tenant_id_column is None:
        return statement, query_params

    tenant_id = _tenant_id()
    use_dict = isinstance(query_params, dict)

    if TENANT_FILTER_TOKEN in statement:
        filter_expr = f"{tenant_id_column} = %(tenant_id)s" if use_dict else f"{tenant_id_column} = %s"
        statement = statement.replace(TENANT_FILTER_TOKEN, filter_expr)
    else:
        logger.debug("Auto-injecting tenant filter into SQL without placeholder")
        statement = _inject_tenant_filter(statement, tenant_id_column, use_dict)

    if use_dict:
        query_params["tenant_id"] = tenant_id
    else:
        query_params.append(tenant_id)

    return statement, query_params


def query_df(
    sql: str,
    params: list | tuple | dict | None = None,
    tenant_id_column: str | None = "tenant_id",
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
    tenant_id_column: str | None = "tenant_id",
) -> list[dict[str, Any]]:
    frame = query_df(sql, params=params, tenant_id_column=tenant_id_column)
    return normalize_frame_nulls(frame).to_dict(orient="records")


def query_scalar(
    sql: str,
    params: list | tuple | dict | None = None,
    tenant_id_column: str | None = "tenant_id",
    default: Any = None,
) -> Any:
    frame = query_df(sql, params=params, tenant_id_column=tenant_id_column)
    if frame.empty or frame.shape[1] == 0:
        return default
    value = frame.iloc[0, 0]
    normalized = normalize_frame_nulls(pd.DataFrame({"value": [value]})).iloc[0, 0]
    return default if normalized is None else normalized

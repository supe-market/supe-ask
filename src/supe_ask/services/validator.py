from __future__ import annotations

import ast
import re


ALLOWED_IMPORTS = {
    "pandas",
    "plotly",
    "plotly.express",
    "plotly.graph_objects",
    "supe_lib",
    "supe_lib.db",
    "supe_lib.display",
    "supe_lib.dataframes",
    "supe_lib.charts",
    "supe_lib.metrics",
    "supe_lib.plotting",
    "supe_lib.report",
    "supe_lib.supe",
    "supe_lib.time",
    "supe_lib.math_utils",
    "math",
    "statistics",
    "datetime",
    "json",
    "numpy",
    "concurrent.futures",
}

BLOCKED_NAMES = {"open", "eval", "exec", "compile", "__import__", "input", "exit", "quit"}

BLOCKED_MODULE_PREFIXES = {"os", "sys", "subprocess", "socket", "requests", "httpx", "pathlib", "shutil"}
BLOCKED_SQL_TABLES = {"entity_metric_snapshots"}


class CodeValidationError(ValueError):
    pass


def validate_python_code(code: str) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        raise CodeValidationError(f"Syntax error: {error.msg}") from error

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_IMPORTS and not any(
                    alias.name.startswith(f"{allowed}.") for allowed in ALLOWED_IMPORTS
                ):
                    raise CodeValidationError(f"Import '{alias.name}' is not allowed")
                if any(alias.name == blocked or alias.name.startswith(f"{blocked}.") for blocked in BLOCKED_MODULE_PREFIXES):
                    raise CodeValidationError(f"Import '{alias.name}' is blocked")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module not in ALLOWED_IMPORTS and not any(module.startswith(f"{allowed}.") for allowed in ALLOWED_IMPORTS):
                raise CodeValidationError(f"Import from '{module}' is not allowed")
            if any(module == blocked or module.startswith(f"{blocked}.") for blocked in BLOCKED_MODULE_PREFIXES):
                raise CodeValidationError(f"Import from '{module}' is blocked")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BLOCKED_NAMES:
                raise CodeValidationError(f"Call to '{func.id}' is not allowed")
            if isinstance(func, ast.Name) and func.id == "period_bounds" and len(node.args) > 1:
                raise CodeValidationError(
                    "Use build_period_filter(...) or call period_bounds(period, today=...) with at most one positional argument"
                )
            if isinstance(func, ast.Name) and func.id == "emit_table":
                bad_kwargs = {kw.arg for kw in node.keywords if kw.arg not in {"frame", "title", "max_rows", None}}
                if bad_kwargs:
                    raise CodeValidationError(
                        f"emit_table() does not accept keyword argument(s): {', '.join(sorted(bad_kwargs))}. "
                        "Signature is emit_table(frame, title, max_rows). To rename columns use df.rename(columns={{...}}) before calling emit_table."
                    )
            if isinstance(func, ast.Name) and func.id == "query_df":
                kwarg_names = {kw.arg for kw in node.keywords}
                if "tenant_id_column" not in kwarg_names:
                    # Try to resolve the SQL string from the first positional arg or 'sql' kwarg.
                    sql_node = None
                    if node.args:
                        sql_node = node.args[0]
                    else:
                        for kw in node.keywords:
                            if kw.arg == "sql":
                                sql_node = kw.value
                                break
                    sql_text = None
                    if isinstance(sql_node, ast.Constant) and isinstance(sql_node.value, str):
                        sql_text = sql_node.value
                    elif isinstance(sql_node, ast.JoinedStr):
                        # f-string: concatenate any constant parts we can read
                        parts = [v.value for v in sql_node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
                        sql_text = " ".join(parts)
                    if sql_text and "join" in sql_text.lower():
                        raise CodeValidationError(
                            "query_df() call with a JOIN query is missing tenant_id_column. "
                            "Pass tenant_id_column=\"<fact_alias>.tenant_id\" (e.g. tenant_id_column=\"so.tenant_id\") "
                            "to avoid psycopg2.errors.AmbiguousColumn at runtime."
                        )
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in BLOCKED_MODULE_PREFIXES:
                raise CodeValidationError(f"Access to '{node.value.id}.{node.attr}' is not allowed")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = node.value.lower()
            for table_name in BLOCKED_SQL_TABLES:
                if table_name in lowered:
                    raise CodeValidationError(
                        f"Querying '{table_name}' is not allowed for Ask-generated business analysis; recalculate from raw fact tables"
                    )
            # Catch numeric aliases like `COUNT(*) AS 0 AS total_orders` — always a syntax error in PostgreSQL.
            if re.search(r"\bAS\s+\d+\b", node.value, re.IGNORECASE):
                raise CodeValidationError(
                    "SQL contains a numeric column alias (e.g. AS 0). Column aliases must be valid identifiers. "
                    "Wrong: COUNT(*) AS 0 AS total_orders. Correct: COUNT(*) AS total_orders."
                )

from __future__ import annotations

import ast


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

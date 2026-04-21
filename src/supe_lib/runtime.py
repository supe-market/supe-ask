from __future__ import annotations

import builtins
import contextlib
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from .charts import bar_chart, line_chart, pie_chart, waterfall_chart
from .db import query_df, query_records, query_scalar
from .display import display as emit_display
from .metrics import growth_rate, percent_delta, safe_percent
from .plotting import figure_title
from .report import (
    emit_highlights,
    emit_kpi_card,
    emit_log_lines,
    emit_markdown,
    emit_metric,
    emit_plotly,
    emit_section,
    emit_summary,
    emit_table,
    fmt_currency,
    fmt_number,
    fmt_percent,
    progress,
)
from .supe import build_kpi_summary, build_period_filter, build_scope_filter
from .time import period_bounds


ALLOWED_IMPORT_ROOTS = {
    "concurrent",
    "datetime",
    "json",
    "math",
    "numpy",
    "pandas",
    "plotly",
    "statistics",
    "supe_lib",
}

LOG_PREFIX_PATTERN = re.compile(r"^\s*(Progress|Info|Result|Debug|Warning|Error):\s*(.*)$", re.DOTALL)


def _restricted_import(name: str, globals=None, locals=None, fromlist=(), level: int = 0):
    root = name.split(".")[0]
    if root not in ALLOWED_IMPORT_ROOTS:
        raise ImportError(f"Import '{name}' is not allowed")
    return builtins.__import__(name, globals, locals, fromlist, level)


BASE_SAFE_BUILTINS = {
    "__build_class__": builtins.__build_class__,
    "__import__": _restricted_import,
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "Exception": Exception,
    "filter": filter,
    "float": float,
    "getattr": getattr,
    "hasattr": hasattr,
    "int": int,
    "isinstance": isinstance,
    "iter": iter,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "next": next,
    "object": object,
    "range": range,
    "repr": repr,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "type": type,
    "ValueError": ValueError,
    "zip": zip,
}


@dataclass
class ExecutionContext:
    original_print: Callable[..., None] = builtins.print
    log_lines: list[str] = field(default_factory=list)

    def print(self, *args: Any, sep: str = " ", end: str = "\n", file=None, flush: bool = False) -> None:
        message = sep.join(str(arg) for arg in args)
        normalized = message.rstrip("\n")
        if end:
            normalized = (message + end).rstrip("\n")

        match = LOG_PREFIX_PATTERN.match(normalized)
        if match:
            prefix = match.group(1).lower()
            content = match.group(2).strip()
            if prefix == "progress":
                progress(" ".join(part.strip() for part in content.splitlines() if part.strip()) or normalized)
            else:
                self.log_lines.extend([line for line in normalized.splitlines() if line.strip()])
            return

        self.original_print(*args, sep=sep, end=end, file=file, flush=flush)

    def display(self, value: Any, title: str | None = None) -> None:
        emit_display(value, title=title)

    def flush_logs(self) -> None:
        emit_log_lines(self.log_lines)


@contextlib.contextmanager
def _patched_plotly_show():
    try:
        from plotly.basedatatypes import BaseFigure
    except Exception:
        yield
        return

    original_show = BaseFigure.show

    def _capture_show(figure: BaseFigure, *args, **kwargs):
        emit_plotly(figure, title=kwargs.get("title") or figure_title(figure, "Chart"))

    BaseFigure.show = _capture_show
    try:
        yield
    finally:
        BaseFigure.show = original_show


def _safe_builtins(context: ExecutionContext) -> dict[str, Any]:
    safe_builtins = dict(BASE_SAFE_BUILTINS)
    safe_builtins["display"] = context.display
    safe_builtins["print"] = context.print
    return safe_builtins


def execute_user_code(user_code: str) -> None:
    context = ExecutionContext()
    globals_dict = {
        "__builtins__": _safe_builtins(context),
        "__name__": "__main__",
        # Pre-injected libraries
        "pd": pd,
        "px": px,
        "go": go,
        "ThreadPoolExecutor": ThreadPoolExecutor,
        # Data access
        "query_df": query_df,
        "query_records": query_records,
        "query_scalar": query_scalar,
        # Report / emit functions
        "emit_markdown": emit_markdown,
        "emit_metric": emit_metric,
        "emit_table": emit_table,
        "emit_plotly": emit_plotly,
        "emit_kpi_card": emit_kpi_card,
        "emit_section": emit_section,
        "emit_summary": emit_summary,
        "emit_highlights": emit_highlights,
        "progress": progress,
        # Formatting
        "fmt_currency": fmt_currency,
        "fmt_percent": fmt_percent,
        "fmt_number": fmt_number,
        # Metrics
        "safe_percent": safe_percent,
        "percent_delta": percent_delta,
        "growth_rate": growth_rate,
        # Charts
        "bar_chart": bar_chart,
        "line_chart": line_chart,
        "pie_chart": pie_chart,
        "waterfall_chart": waterfall_chart,
        # Scope / period / KPI helpers
        "build_scope_filter": build_scope_filter,
        "build_period_filter": build_period_filter,
        "build_kpi_summary": build_kpi_summary,
        "period_bounds": period_bounds,
        # Display
        "display": context.display,
    }
    locals_dict = globals_dict

    with _patched_plotly_show():
        try:
            exec(compile(user_code, "supe_ask_generated.py", "exec"), globals_dict, locals_dict)
        finally:
            context.flush_logs()

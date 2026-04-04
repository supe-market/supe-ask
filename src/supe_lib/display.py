from __future__ import annotations

import json
from typing import Any

import pandas as pd

from .dataframes import series_to_frame
from .plotting import figure_title, is_plotly_figure
from .report import emit_markdown, emit_plotly, emit_table


def _json_markdown(value: Any) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=True, indent=2, default=str) + "\n```"


def display(value: Any, title: str | None = None) -> None:
    if isinstance(value, pd.DataFrame):
        emit_table(value, title=title or "Table")
        return

    if isinstance(value, pd.Series):
        emit_table(series_to_frame(value), title=title or f"Series {value.name or 'value'}")
        return

    if is_plotly_figure(value):
        emit_plotly(value, title=title or figure_title(value, "Chart"))
        return

    if isinstance(value, str):
        emit_markdown(value, title=title or "Notes")
        return

    if isinstance(value, dict):
        if value and all(not isinstance(item, (dict, list, tuple)) for item in value.values()):
            emit_table(pd.DataFrame([{"key": key, "value": item} for key, item in value.items()]), title=title or "Structured data")
            return
        emit_markdown(_json_markdown(value), title=title or "Structured data")
        return

    if isinstance(value, (list, tuple)):
        if value and all(isinstance(item, dict) for item in value):
            emit_table(pd.DataFrame(list(value)), title=title or "Structured data")
            return
        if value and all(not isinstance(item, (dict, list, tuple)) for item in value):
            emit_table(pd.DataFrame({"value": list(value)}), title=title or "Structured data")
            return
        emit_markdown(_json_markdown(list(value)), title=title or "Structured data")
        return

    emit_markdown(f"```text\n{value}\n```", title=title or "Output")

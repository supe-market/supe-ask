from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from typing import Any

import pandas as pd

from .dataframes import frame_records, summarize_frame
from .plotting import figure_title, normalize_plotly_figure


EVENT_PREFIX = os.getenv("SUPE_ASK_EVENT_PREFIX", "__SUPE_ASK_EVENT__")
DEFAULT_TABLE_TITLE = "Table"
DEFAULT_MARKDOWN_TITLE = "Summary"
DEFAULT_METRIC_TITLE = "Metric"
DEFAULT_PLOT_TITLE = "Chart"
DEFAULT_LOG_TITLE = "Execution log"


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "item") and callable(value.item):
        try:
            return value.item()
        except Exception:
            return str(value)
    return str(value)


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(EVENT_PREFIX + json.dumps(payload, ensure_ascii=True, default=_json_default) + "\n")
    sys.stdout.flush()


def _artifact_payload(artifact_type: str, title: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "artifact",
        "payload": {
            "artifact_type": artifact_type,
            "title": title,
            "payload": payload,
        },
    }


def progress(message: str) -> None:
    _emit({"type": "progress", "payload": {"message": str(message)}})


def emit_markdown(markdown: str, title: str = DEFAULT_MARKDOWN_TITLE) -> None:
    _emit(_artifact_payload("markdown", title or DEFAULT_MARKDOWN_TITLE, {"markdown": markdown}))


def emit_metric(label: str, value: Any, tone: str = "neutral", title: str = DEFAULT_METRIC_TITLE) -> None:
    _emit(
        _artifact_payload(
            "metric",
            title or DEFAULT_METRIC_TITLE,
            {"label": label, "value": value, "tone": tone},
        )
    )


def emit_table(frame: pd.DataFrame, title: str = DEFAULT_TABLE_TITLE, max_rows: int = 50) -> None:
    table_payload = frame_records(frame, max_rows=max_rows)
    table_payload["summary"] = summarize_frame(frame)
    _emit(_artifact_payload("table", title or DEFAULT_TABLE_TITLE, table_payload))


def emit_plotly(fig: Any, title: str | None = None) -> None:
    normalized = normalize_plotly_figure(fig, title=title)
    _emit(
        _artifact_payload(
            "plotly",
            title or figure_title(normalized, DEFAULT_PLOT_TITLE),
            normalized.to_plotly_json(),
        )
    )


def emit_log_lines(lines: list[str], title: str = DEFAULT_LOG_TITLE) -> None:
    normalized_lines = [str(line) for line in lines if str(line).strip()]
    if not normalized_lines:
        return
    _emit(_artifact_payload("log", title or DEFAULT_LOG_TITLE, {"lines": normalized_lines}))


# ── Enhanced report helpers ───────────────────────────────────────

def emit_kpi_card(
    label: str | None = None,
    current: float | int | str | None = None,
    previous: float | int | str | None = None,
    *,
    unit: str = "number",
    title: str = "",
    benchmark: str = "",
    value: float | int | str | None = None,
    delta_value: str | None = None,
    delta_label: str | None = None,
    positive_good: bool | None = None,
    tone: str | None = None,
) -> None:
    """Emit a rich KPI metric card with optional comparison.

    ``unit`` controls frontend formatting: ``"currency"``, ``"percent"``,
    or ``"number"`` (default).
    """
    from .metrics import percent_delta as _pct_delta

    display_label = str(label or title or "Metric")
    display_value = current if current is not None else value
    resolved_tone = str(tone or "neutral")
    pct_delta = None
    try:
        if previous is not None and float(previous) != 0 and display_value is not None:
            pct_delta = _pct_delta(float(display_value), float(previous))
            resolved_tone = "positive" if pct_delta > 0 else "negative" if pct_delta < 0 else "neutral"
    except Exception:
        pct_delta = None

    if delta_value and tone is None and positive_good is not None:
        resolved_tone = "positive" if positive_good else "negative"

    payload: dict[str, Any] = {
        "label": display_label,
        "value": display_value,
        "tone": resolved_tone,
        "unit": unit,
    }
    if previous is not None:
        payload["previous"] = previous
        payload["percentDelta"] = pct_delta
    if benchmark:
        payload["benchmark"] = benchmark
    if delta_value:
        payload["deltaText"] = str(delta_value)
    if delta_label:
        payload["deltaLabel"] = str(delta_label)

    _emit(_artifact_payload("metric", title or display_label, payload))


def emit_section(title: str, subtitle: str = "") -> None:
    """Emit a section divider that structures the report visually."""
    _emit({
        "type": "artifact",
        "payload": {
            "artifact_type": "section",
            "title": title,
            "payload": {"title": title, "subtitle": subtitle},
        },
    })


def emit_summary(text: str, title: str = "Summary") -> None:
    """Emit a formatted summary block — suitable for executive overviews."""
    emit_markdown(text, title=title)


def fmt_currency(value: float | int, symbol: str = "\u20b9", decimals: int = 0) -> str:
    """Format a number as currency with Indian-style comma grouping."""
    if value >= 1_00_00_000:
        return f"{symbol}{value / 1_00_00_000:,.2f} Cr"
    if value >= 1_00_000:
        return f"{symbol}{value / 1_00_000:,.2f} L"
    return f"{symbol}{value:,.{decimals}f}"


def fmt_percent(value: float, decimals: int = 1) -> str:
    """Format a number as a percentage string."""
    return f"{value:,.{decimals}f}%"


def fmt_number(value: float | int, decimals: int = 0) -> str:
    """Format a number with comma grouping."""
    return f"{value:,.{decimals}f}"

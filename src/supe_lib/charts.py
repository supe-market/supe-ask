"""High-level chart builder helpers for consistent Ask reports.

Generated code can call these instead of raw Plotly, ensuring every
chart in a report has the same fonts, colors, margins, and layout.
Inspired by ScalarField's scalarlib which wraps Plotly with standard
themes and auto-display.
"""

from __future__ import annotations

from typing import Any, Sequence

import pandas as pd

from .plotting import normalize_plotly_figure
from .report import emit_plotly

# Supe brand palette — matches the frontend ASK_CHART_COLORS
SUPE_COLORS = ["#1d4ed8", "#0f766e", "#b45309", "#be123c", "#7c3aed", "#0369a1"]


def _ensure_plotly():
    import plotly.graph_objects as go
    return go


def bar_chart(
    df: pd.DataFrame | None = None,
    x: str | None = None,
    y: str | list[str] | None = None,
    title: str = "Bar Chart",
    *,
    color: str | list[str] | None = None,
    barmode: str = "group",
    data_frame: pd.DataFrame | None = None,
    labels: dict[str, str] | None = None,
    orientation: str = "v",
    show: bool = True,
) -> Any:
    """Build and optionally emit a bar chart from a DataFrame."""
    go = _ensure_plotly()
    df = data_frame if data_frame is not None else df
    if df is None or x is None or y is None:
        raise ValueError("bar_chart requires a DataFrame via df or data_frame")
    fig = go.Figure()
    axis_labels = labels or {}
    if orientation == "h" and isinstance(y, str):
        fig.add_trace(go.Bar(
            x=df[x],
            y=df[y],
            orientation="h",
            name=axis_labels.get(x) or x.replace("_", " ").title(),
            marker_color=(color if isinstance(color, str) else (color or SUPE_COLORS))[0],
        ))
        fig.update_layout(
            barmode=barmode,
            title=title,
            xaxis_title=axis_labels.get(x) or x.replace("_", " ").title(),
            yaxis_title=axis_labels.get(y) or y.replace("_", " ").title(),
        )
        fig = normalize_plotly_figure(fig, title=title)
        if show:
            emit_plotly(fig, title=title)
        return fig

    y_cols = [y] if isinstance(y, str) else list(y)
    colors = [color] if isinstance(color, str) else (color or SUPE_COLORS)
    for i, col in enumerate(y_cols):
        fig.add_trace(go.Bar(
            x=df[x],
            y=df[col],
            name=col.replace("_", " ").title(),
            marker_color=colors[i % len(colors)],
        ))
    fig.update_layout(
        barmode=barmode,
        title=title,
        xaxis_title=axis_labels.get(x) or x.replace("_", " ").title(),
        yaxis_title=axis_labels.get(y_cols[0]) or y_cols[0].replace("_", " ").title(),
    )
    fig = normalize_plotly_figure(fig, title=title)
    if show:
        emit_plotly(fig, title=title)
    return fig


def line_chart(
    df: pd.DataFrame | None = None,
    x: str | None = None,
    y: str | list[str] | None = None,
    title: str = "Line Chart",
    *,
    color: str | list[str] | None = None,
    data_frame: pd.DataFrame | None = None,
    labels: dict[str, str] | None = None,
    show: bool = True,
) -> Any:
    """Build and optionally emit a line chart from a DataFrame."""
    go = _ensure_plotly()
    df = data_frame if data_frame is not None else df
    if df is None or x is None or y is None:
        raise ValueError("line_chart requires a DataFrame via df or data_frame")
    fig = go.Figure()
    y_cols = [y] if isinstance(y, str) else list(y)
    colors = [color] if isinstance(color, str) else (color or SUPE_COLORS)
    axis_labels = labels or {}
    for i, col in enumerate(y_cols):
        fig.add_trace(go.Scatter(
            x=df[x],
            y=df[col],
            mode="lines+markers",
            name=col.replace("_", " ").title(),
            line={"color": colors[i % len(colors)], "width": 2},
            marker={"size": 5},
        ))
    fig.update_layout(
        title=title,
        xaxis_title=axis_labels.get(x) or x.replace("_", " ").title(),
        yaxis_title=axis_labels.get(y_cols[0]) or y_cols[0].replace("_", " ").title(),
    )
    fig = normalize_plotly_figure(fig, title=title)
    if show:
        emit_plotly(fig, title=title)
    return fig


def pie_chart(
    df: pd.DataFrame,
    labels: str,
    values: str,
    title: str = "Pie Chart",
    *,
    show: bool = True,
) -> Any:
    """Build and optionally emit a pie chart."""
    go = _ensure_plotly()
    fig = go.Figure(go.Pie(
        labels=df[labels],
        values=df[values],
        marker={"colors": SUPE_COLORS},
        textinfo="label+percent",
        hole=0.35,
    ))
    fig.update_layout(title=title)
    fig = normalize_plotly_figure(fig, title=title)
    if show:
        emit_plotly(fig, title=title)
    return fig


def waterfall_chart(
    categories: Sequence[str],
    values: Sequence[float],
    title: str = "Waterfall",
    *,
    show: bool = True,
) -> Any:
    """Build and optionally emit a waterfall chart."""
    go = _ensure_plotly()
    measures = ["relative"] * len(values)
    measures[-1] = "total"
    fig = go.Figure(go.Waterfall(
        x=list(categories),
        y=list(values),
        measure=measures,
        increasing={"marker": {"color": "#0f766e"}},
        decreasing={"marker": {"color": "#be123c"}},
        totals={"marker": {"color": "#1d4ed8"}},
    ))
    fig.update_layout(title=title)
    fig = normalize_plotly_figure(fig, title=title)
    if show:
        emit_plotly(fig, title=title)
    return fig

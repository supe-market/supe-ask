from __future__ import annotations

from typing import Any

DEFAULT_MARGIN = {"l": 32, "r": 24, "t": 56, "b": 32}


def is_plotly_figure(value: Any) -> bool:
    try:
        from plotly.basedatatypes import BaseFigure
    except ImportError:
        return False
    return isinstance(value, BaseFigure)


def figure_title(fig: Any, fallback: str = "Chart") -> str:
    title = getattr(getattr(fig, "layout", None), "title", None)
    text = getattr(title, "text", None)
    return str(text) if text else fallback


def normalize_plotly_figure(fig: Any, title: str | None = None) -> Any:
    import plotly.graph_objects as go

    normalized = go.Figure(fig)
    normalized.update_layout(
        template="plotly_white",
        paper_bgcolor="white",
        plot_bgcolor="white",
        margin=DEFAULT_MARGIN,
        title=title or figure_title(normalized, "Chart"),
    )
    return normalized

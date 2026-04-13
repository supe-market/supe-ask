from __future__ import annotations

import base64
import json
import math
import struct
from typing import Any

DEFAULT_MARGIN = {"l": 32, "r": 24, "t": 56, "b": 32}
_TYPED_ARRAY_FORMATS = {
    "b1": ("?", 1),
    "f4": ("f", 4),
    "f8": ("d", 8),
    "i1": ("b", 1),
    "i2": ("h", 2),
    "i4": ("i", 4),
    "i8": ("q", 8),
    "u1": ("B", 1),
    "u2": ("H", 2),
    "u4": ("I", 4),
    "u8": ("Q", 8),
}


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


def serialize_plotly_figure(fig: Any, title: str | None = None) -> dict[str, Any]:
    from plotly.utils import PlotlyJSONEncoder

    normalized = normalize_plotly_figure(fig, title=title)
    payload = json.loads(
        json.dumps(
            normalized.to_plotly_json(),
            cls=PlotlyJSONEncoder,
            ensure_ascii=True,
        )
    )
    return _materialize_plotly_value(payload)


def _materialize_plotly_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_materialize_plotly_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "dtype" in value and "bdata" in value:
        decoded = _decode_typed_array(value)
        if decoded is not value:
            return decoded
    return {key: _materialize_plotly_value(item) for key, item in value.items()}


def _decode_typed_array(value: dict[str, Any]) -> Any:
    dtype = str(value.get("dtype") or "").strip().lower()
    encoded = value.get("bdata")
    if dtype not in _TYPED_ARRAY_FORMATS or not isinstance(encoded, str):
        return value

    format_code, byte_width = _TYPED_ARRAY_FORMATS[dtype]
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        return value
    if not raw or len(raw) % byte_width != 0:
        return value

    count = len(raw) // byte_width
    try:
        unpacked = list(struct.unpack(f"<{count}{format_code}", raw))
    except Exception:
        return value

    shape = value.get("shape")
    if isinstance(shape, list) and shape and all(isinstance(item, int) and item >= 0 for item in shape):
        expected = math.prod(shape)
        if expected == len(unpacked):
            return _reshape_typed_array(unpacked, shape)
    return unpacked


def _reshape_typed_array(values: list[Any], shape: list[int]) -> Any:
    if not shape:
        return values
    if len(shape) == 1:
        return values[: shape[0]]
    step = math.prod(shape[1:])
    return [
        _reshape_typed_array(values[index * step : (index + 1) * step], shape[1:])
        for index in range(shape[0])
    ]

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return value
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if hasattr(value, "item") and callable(value.item):
        try:
            return value.item()
        except Exception:
            return str(value)
    return value


def normalize_frame_nulls(frame: pd.DataFrame) -> pd.DataFrame:
    normalized = frame.copy()
    for column in normalized.columns:
        normalized[column] = normalized[column].map(_normalize_scalar)
    return normalized


def truncate_frame(frame: pd.DataFrame, max_rows: int = 50) -> pd.DataFrame:
    return frame.head(max_rows).copy()


def series_to_frame(series: pd.Series) -> pd.DataFrame:
    value_name = str(series.name or "value")
    index_name = str(series.index.name or "index")
    return series.rename(value_name).reset_index().rename(columns={series.index.name or "index": index_name})


def frame_records(frame: pd.DataFrame, max_rows: int = 50) -> dict[str, Any]:
    table = normalize_frame_nulls(truncate_frame(frame, max_rows=max_rows))
    return {
        "columns": [str(column) for column in table.columns],
        "rows": table.to_dict(orient="records"),
        "rowCount": int(len(frame.index)),
    }


def summarize_frame(frame: pd.DataFrame) -> dict[str, Any]:
    numeric_columns = [str(column) for column in frame.select_dtypes(include=["number", "bool"]).columns]
    return {
        "rowCount": int(len(frame.index)),
        "columnCount": int(len(frame.columns)),
        "numericColumns": numeric_columns,
        "numericColumnCount": len(numeric_columns),
        "nullCount": int(frame.isna().sum().sum()),
    }


def coerce_numeric_columns(frame: pd.DataFrame, columns: list[str] | None = None) -> pd.DataFrame:
    normalized = frame.copy()
    target_columns = columns or [str(column) for column in normalized.columns]
    for column in target_columns:
        if column in normalized.columns:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    return normalized


def sort_frame(frame: pd.DataFrame, by: str | list[str], ascending: bool = False) -> pd.DataFrame:
    return frame.sort_values(by=by, ascending=ascending).reset_index(drop=True)


def top_n(frame: pd.DataFrame, by: str, n: int = 10) -> pd.DataFrame:
    return sort_frame(frame, by=by, ascending=False).head(n).reset_index(drop=True)


def bottom_n(frame: pd.DataFrame, by: str, n: int = 10) -> pd.DataFrame:
    return sort_frame(frame, by=by, ascending=True).head(n).reset_index(drop=True)

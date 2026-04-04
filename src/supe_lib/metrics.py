from __future__ import annotations

from typing import Any

import pandas as pd


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    if denominator == 0:
        return default
    return numerator / denominator


def safe_percent(numerator: float, denominator: float) -> float:
    return round(safe_divide(numerator, denominator) * 100, 2)


def absolute_delta(current: float, previous: float) -> float:
    return round(current - previous, 2)


def percent_delta(current: float, previous: float) -> float:
    if previous == 0:
        return 0.0
    return round(((current - previous) / previous) * 100, 2)


def growth_rate(current: float, previous: float) -> float:
    return percent_delta(current, previous)


def share_of_total(value: float, total: float) -> float:
    return safe_percent(value, total)


def dense_rank(series: pd.Series, ascending: bool = False) -> pd.Series:
    return series.rank(method="dense", ascending=ascending).astype("Int64")


def kpi_summary(label: str, current: float, previous: float) -> dict[str, Any]:
    delta = absolute_delta(current, previous)
    percent_change = percent_delta(current, previous)
    tone = "positive" if delta > 0 else "negative" if delta < 0 else "neutral"
    return {
        "label": label,
        "current": current,
        "previous": previous,
        "delta": delta,
        "percentDelta": percent_change,
        "tone": tone,
    }

from __future__ import annotations

from typing import Any

from .metrics import kpi_summary
from .time import period_bounds


DEFAULT_SCOPE_COLUMNS = {
    "national": None,
    "country": None,
    "zone": "zone",
    "region": "region",
    "area": "area",
    "distributor": "distributor_id",
    "beat": "beat_id",
    "salesman": "salesman_id",
}


def merge_params(*param_sets: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for param_set in param_sets:
        merged.update(param_set)
    return merged


def sql_and(*clauses: str) -> str:
    normalized = [f"({clause})" for clause in clauses if clause and clause != "1=1"]
    return " and ".join(normalized) if normalized else "1=1"


def build_scope_filter(
    level: str,
    value: Any,
    column_map: dict[str, str | None] | None = None,
    param_name: str = "scope_value",
) -> tuple[str, dict[str, Any]]:
    normalized_level = (level or "national").strip().lower()
    if normalized_level in {"national", "country"} or value in {None, "", "all", "all_india"}:
        return "1=1", {}

    column = (column_map or DEFAULT_SCOPE_COLUMNS).get(normalized_level)
    if not column:
        raise ValueError(f"Unsupported scope level: {level}")
    return f"{column} = %({param_name})s", {param_name: value}


def build_period_filter(
    date_column: str,
    period: str,
    today: Any = None,
    start_param: str = "period_start",
    end_param: str = "period_end",
) -> tuple[str, dict[str, Any]]:
    start_date, end_date = period_bounds(period, today=today)
    return (
        f"{date_column} >= %({start_param})s and {date_column} <= %({end_param})s",
        {start_param: start_date.isoformat(), end_param: end_date.isoformat()},
    )


def build_kpi_summary(label: str, current: float, previous: float) -> dict[str, Any]:
    return kpi_summary(label, current, previous)

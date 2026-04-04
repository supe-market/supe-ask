from __future__ import annotations

import re
from datetime import date, datetime, timedelta


LAST_N_DAYS_PATTERN = re.compile(r"^last_(\d+)_days$")


def as_date(value: date | datetime | str | None = None) -> date:
    if value is None:
        return datetime.utcnow().date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


def month_start(value: date | datetime | str | None = None) -> date:
    current = as_date(value)
    return current.replace(day=1)


def quarter_start(value: date | datetime | str | None = None) -> date:
    current = as_date(value)
    quarter_month = ((current.month - 1) // 3) * 3 + 1
    return current.replace(month=quarter_month, day=1)


def year_start(value: date | datetime | str | None = None) -> date:
    current = as_date(value)
    return current.replace(month=1, day=1)


def period_bounds(period: str, today: date | datetime | str | None = None) -> tuple[date, date]:
    current = as_date(today)
    normalized = period.strip().lower().replace("-", "_").replace(" ", "_")

    if normalized in {"mtd", "month_to_date"}:
        return month_start(current), current
    if normalized in {"qtd", "quarter_to_date"}:
        return quarter_start(current), current
    if normalized in {"ytd", "year_to_date"}:
        return year_start(current), current
    if normalized in {"last_7d", "last_7_days"}:
        return current - timedelta(days=6), current
    if normalized in {"last_30d", "last_30_days"}:
        return current - timedelta(days=29), current
    if normalized in {"last_90d", "last_90_days"}:
        return current - timedelta(days=89), current

    match = LAST_N_DAYS_PATTERN.match(normalized)
    if match:
        total_days = max(int(match.group(1)), 1)
        return current - timedelta(days=total_days - 1), current

    raise ValueError(f"Unsupported period: {period}")


def period_params(period: str, today: date | datetime | str | None = None) -> dict[str, str]:
    start_date, end_date = period_bounds(period, today=today)
    return {
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
    }

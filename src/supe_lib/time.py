from __future__ import annotations

import re
from datetime import date, datetime, timedelta


LAST_N_DAYS_PATTERN = re.compile(r"^last_(\d+)_days$")


def _normalize_period_label(value: str | None) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _is_supported_period_label(value: object) -> bool:
    normalized = _normalize_period_label(str(value))
    return normalized in {
        "mtd",
        "month_to_date",
        "qtd",
        "quarter_to_date",
        "ytd",
        "year_to_date",
        "last_7d",
        "last_7_days",
        "last_30d",
        "last_30_days",
        "last_90d",
        "last_90_days",
    } or bool(LAST_N_DAYS_PATTERN.match(normalized))


def _looks_like_date(value: object) -> bool:
    if isinstance(value, (date, datetime)):
        return True
    if value is None:
        return False
    try:
        datetime.fromisoformat(str(value))
        return True
    except Exception:
        return False


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
    # Some generated scripts mistakenly swap the arguments as
    # period_bounds(today_iso, "mtd"). Recover that common misuse instead of
    # crashing the run on an avoidable ValueError.
    if _is_supported_period_label(today) and _looks_like_date(period):
        period, today = str(today), period
    elif _is_supported_period_label(today) and not _is_supported_period_label(period):
        period, today = str(today), None

    current = as_date(today)
    normalized = _normalize_period_label(period)

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

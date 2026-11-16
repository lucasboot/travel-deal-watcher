from __future__ import annotations

import calendar
import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .locations import airport_pairs
from .models import WatcherConfig

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlannedQuery:
    provider: str
    origin: str
    destination: str
    departure_month: str
    return_month: str
    trip_days: int | None = None
    query_type: str = "search"
    outbound_date: str | None = None
    return_date: str | None = None

    @property
    def key(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


def month_at(offset: int, today: date | None = None) -> tuple[int, int]:
    base = today or datetime.now(UTC).date()
    ordinal = base.year * 12 + base.month - 1 + offset
    return ordinal // 12, ordinal % 12 + 1


def month_string(offset: int, today: date | None = None) -> str:
    year, month = month_at(offset, today)
    return f"{year:04d}-{month:02d}"


def _possible_month_pair(
    depart_offset: int, return_offset: int, config: WatcherConfig, today: date | None = None
) -> bool:
    depart_year, depart_month = month_at(depart_offset, today)
    return_year, return_month = month_at(return_offset, today)
    depart_start = date(depart_year, depart_month, 1)
    depart_end = date(depart_year, depart_month, calendar.monthrange(depart_year, depart_month)[1])
    return_start = date(return_year, return_month, 1)
    return_end = date(return_year, return_month, calendar.monthrange(return_year, return_month)[1])
    shortest = (return_start - depart_end).days
    longest = (return_end - depart_start).days
    return longest >= config.trip.duration_min_days and shortest <= config.trip.duration_max_days


def candidate_month_pairs(config: WatcherConfig, today: date | None = None) -> list[tuple[str, str]]:
    result = []
    # Include the month containing the latest permitted return date.
    max_offset = config.trip.months_ahead_max + 2
    for departure_offset in range(config.trip.months_ahead_min, config.trip.months_ahead_max + 1):
        for return_offset in range(departure_offset, max_offset + 1):
            if _possible_month_pair(departure_offset, return_offset, config, today):
                result.append((month_string(departure_offset, today), month_string(return_offset, today)))
    return result


def _rotate(values: list[Any], cursor: int, count: int) -> tuple[list[Any], int]:
    if not values or count <= 0:
        return [], cursor
    start = cursor % len(values)
    selected = [values[(start + index) % len(values)] for index in range(min(count, len(values)))]
    return selected, (start + len(selected)) % len(values)


def plan_travelpayouts(
    config: WatcherConfig, state: dict[str, Any], today: date | None = None
) -> list[PlannedQuery]:
    pairs = airport_pairs(config)
    months = candidate_month_pairs(config, today)
    candidates = [
        PlannedQuery("travelpayouts", origin, destination, dep_month, ret_month)
        for origin, destination in pairs
        for dep_month, ret_month in months
    ]
    cursor = int(state["cursors"].get("travelpayouts", 0))
    choices, next_cursor = _rotate(
        candidates, cursor, config.schedule.travelpayouts_max_queries_per_cycle
    )
    state["cursors"]["travelpayouts"] = next_cursor
    return choices


def plan_serpapi(
    config: WatcherConfig, state: dict[str, Any], today: date | None = None
) -> list[PlannedQuery]:
    base_date = today or datetime.now(UTC).date()
    pairs = airport_pairs(config)
    durations = config.trip.durations_to_sample_days
    candidates: list[PlannedQuery] = []
    for offset in range(config.trip.months_ahead_min, config.trip.months_ahead_max + 1):
        start_year, start_month = month_at(offset, base_date)
        end_year, end_month = month_at(offset, base_date)
        start = date(start_year, start_month, 1)
        end = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
        for origin, destination in pairs:
            for duration in durations:
                candidates.append(
                    PlannedQuery(
                        provider="serpapi",
                        origin=origin,
                        destination=destination,
                        departure_month=start.isoformat(),
                        return_month=end.isoformat(),
                        trip_days=duration,
                        query_type="calendar",
                    )
                )
    cursor = int(state["cursors"].get("serpapi", 0))
    # A calendar query can produce multiple dates, so each selected item covers one route/month/duration.
    choices, next_cursor = _rotate(candidates, cursor, 2)
    state["cursors"]["serpapi"] = next_cursor
    return choices


def serapi_queries_used(state: dict[str, Any], month: str | None = None) -> int:
    current_month = month or datetime.now(UTC).strftime("%Y-%m")
    return sum(
        1
        for record in state["query_log"]
        if record.get("provider") == "serpapi" and record.get("created_at", "").startswith(current_month)
    )


def reserve_query(
    state: dict[str, Any], query: PlannedQuery, monthly_budget: int
) -> tuple[bool, str]:
    now = datetime.now(UTC)
    expiry = state["query_cache"].get(query.key)
    if expiry and expiry > now.isoformat():
        return False, "cached"
    if query.provider == "serpapi" and serapi_queries_used(state) >= monthly_budget:
        return False, "monthly_budget_exhausted"
    state["query_log"].append(
        {
            "provider": query.provider,
            "query_type": query.query_type,
            "query_key": query.key,
            "status": "reserved",
            "created_at": now.isoformat(),
        }
    )
    # Conservative local cache; the upstream's cache may be shorter or longer.
    cache_duration = timedelta(hours=24 if query.provider == "travelpayouts" else 1)
    state["query_cache"][query.key] = (now + cache_duration).isoformat()
    return True, "reserved"


def plan_details_query(
    calendar_query: PlannedQuery,
    outbound_date: str,
    return_date: str,
    direction: str,
) -> PlannedQuery:
    return PlannedQuery(
        provider="serpapi",
        origin=calendar_query.origin,
        destination=calendar_query.destination,
        departure_month=calendar_query.departure_month,
        return_month=calendar_query.return_month,
        trip_days=calendar_query.trip_days,
        query_type=f"{direction}_details",
        outbound_date=outbound_date,
        return_date=return_date,
    )


def mark_query_result(state: dict[str, Any], query: PlannedQuery, status: str) -> None:
    for record in reversed(state["query_log"]):
        if record.get("query_key") == query.key and record.get("status") == "reserved":
            record["status"] = status
            break


def is_due(state: dict[str, Any], task: str, interval: timedelta, now: datetime | None = None) -> bool:
    current = now or datetime.now(UTC)
    last = state["runs"].get(task)
    if not last:
        return True
    try:
        timestamp = datetime.fromisoformat(last)
    except ValueError:
        return True
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return current - timestamp >= interval


def record_run(state: dict[str, Any], task: str, now: datetime | None = None) -> None:
    state["runs"][task] = (now or datetime.now(UTC)).astimezone(UTC).isoformat()

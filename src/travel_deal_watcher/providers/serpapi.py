from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any

from ..models import FareObservation, FlightSegment, Layover, WatcherConfig
from ..planner import PlannedQuery
from .base import (
    HttpProvider,
    ProviderConfigurationError,
    datetime_from_api,
    iso_date,
)

LOGGER = logging.getLogger(__name__)
ACCOUNT_URL = "https://serpapi.com/account.json"
SEARCH_URL = "https://serpapi.com/search.json"
CABIN_CLASS = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}


class SerpApiProvider(HttpProvider):
    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(name="serpapi")
        self.api_key = (api_key if api_key is not None else os.getenv("SERPAPI_API_KEY", "")).strip()
        if not self.api_key:
            raise ProviderConfigurationError("SERPAPI_API_KEY is not configured")

    async def free_quota_available(self, monthly_budget: int) -> tuple[bool, str, int]:
        account = await self.get_json(ACCOUNT_URL, params={"api_key": self.api_key})
        try:
            monthly_price = Decimal(str(account["plan_monthly_price"]))
            remaining = int(account["plan_searches_left"])
        except (KeyError, InvalidOperation, TypeError, ValueError):
            return False, "SerpApi account did not expose a verifiable free plan and quota", 0
        if monthly_price != 0:
            return False, "SerpApi account is not on a zero-cost plan; searches were skipped", 0
        if remaining <= 0:
            return False, "SerpApi free search quota is exhausted", 0
        return True, f"free plan verified; provider searches remaining={remaining}; local cap={monthly_budget}", remaining

    async def search_calendar(
        self, query: PlannedQuery, config: WatcherConfig
    ) -> list[dict[str, Any]]:
        data = await self.get_json(
            SEARCH_URL,
            params={
                "engine": "google_flights_calendar",
                "type": 1,
                "departure_id": query.origin,
                "arrival_id": query.destination,
                "date_range_start": query.departure_month,
                "date_range_end": query.return_month,
                "round_trip_days": query.trip_days,
                "currency": config.app.currency,
                "gl": config.app.locale_country,
                "hl": config.app.locale_language,
                "adults": config.trip.adults,
                "children": config.trip.children,
                "infants_in_seat": config.trip.infants,
                "travel_class": CABIN_CLASS[config.trip.cabin_class],
                "api_key": self.api_key,
            },
        )
        prices = data.get("prices", [])
        if not isinstance(prices, list):
            raise TypeError("SerpApi calendar returned an invalid prices collection")
        return [item for item in prices if isinstance(item, dict)]

    async def search_outbound(
        self,
        *,
        origin: str,
        destination: str,
        departure: str,
        returned: str,
        config: WatcherConfig,
    ) -> dict[str, Any]:
        common = {
            "engine": "google_flights",
            "type": 1,
            "departure_id": origin,
            "arrival_id": destination,
            "outbound_date": departure,
            "return_date": returned,
            "currency": config.app.currency,
            "gl": config.app.locale_country,
            "hl": config.app.locale_language,
            "travel_class": CABIN_CLASS[config.trip.cabin_class],
            "adults": config.trip.adults,
            "children": config.trip.children,
            "infants_in_seat": config.trip.infants,
            "sort_by": 2,
            # SerpApi encodes 0=any, 1=nonstop, 2=up to one stop, 3=up to two stops.
            "stops": (
                config.connections.maximum_stops_per_direction + 1
                if config.connections.maximum_stops_per_direction <= 2
                else 0
            ),
            "layover_duration": f"{int(config.connections.minimum_layover_hours * 60)},{int(config.connections.maximum_layover_hours * 60)}",
            "max_duration": int(config.connections.maximum_direction_duration_hours * 60),
            "no_cache": "true",
            "api_key": self.api_key,
        }
        return await self.get_json(SEARCH_URL, params=common)

    async def search_return(self, departure_token: str) -> dict[str, Any]:
        return await self.get_json(
            SEARCH_URL,
            params={"engine": "google_flights", "departure_token": departure_token, "no_cache": "true", "api_key": self.api_key},
        )

    def build_observation(
        self,
        *,
        outbound_payload: dict[str, Any],
        return_payload: dict[str, Any],
        origin: str,
        destination: str,
        departure: str,
        returned: str,
        calendar_price: Decimal,
        config: WatcherConfig,
    ) -> FareObservation | None:
        outbound_options = _options(outbound_payload)
        if not outbound_options:
            return None
        outbound = next((item for item in outbound_options if item.get("departure_token")), None)
        if outbound is None:
            return None
        return_options = _options(return_payload)
        if not return_options:
            return None
        return_option = return_options[0]
        outbound_segments = _segments(outbound.get("flights", []))
        return_segments = _segments(return_option.get("flights", []))
        if not outbound_segments or not return_segments:
            return None
        if outbound_segments[0].origin != origin or outbound_segments[-1].destination != destination:
            return None
        if return_segments[0].origin != destination or return_segments[-1].destination != origin:
            return None
        # The calendar observation represents a complete round trip; directional prices are never summed.
        outbound_date = iso_date(outbound_segments[0].departure_at)
        return_date = iso_date(return_segments[0].departure_at)
        requested_outbound, requested_return = iso_date(departure), iso_date(returned)
        if outbound_date != requested_outbound or return_date != requested_return:
            return None
        # Departure timestamps are local to each airport and may use different time zones.
        outbound_layovers = _layovers(outbound.get("layovers", []))
        return_layovers = _layovers(return_option.get("layovers", []))
        if not _valid_connections(outbound, outbound_segments, outbound_layovers, config):
            return None
        if not _valid_connections(return_option, return_segments, return_layovers, config):
            return None
        round_trip_price = _decimal_or_none(outbound.get("price"))
        verified_price = round_trip_price or calendar_price
        confidence = "recent_query" if round_trip_price else "cached_indicative"
        notes = ["Consulta recente; preço e disponibilidade devem ser reconfirmados no vendedor."]
        if not round_trip_price:
            notes.append("Preço total veio do calendário em cache; detalhes do itinerário foram consultados agora.")
        now = datetime.now(UTC)
        return FareObservation(
            observation_id=f"serp:{origin}:{destination}:{departure}:{returned}",
            source="serpapi_google_flights",
            confidence=confidence,
            observed_at=now,
            origin=origin,
            destination=destination,
            departure_date=requested_outbound,
            return_date=requested_return,
            total_price=verified_price,
            currency=config.app.currency,
            passengers=config.trip.adults + config.trip.children + config.trip.infants,
            cabin_class=config.trip.cabin_class,
            transfers_outbound=max(0, len(outbound_segments) - 1),
            transfers_return=max(0, len(return_segments) - 1),
            duration_outbound_minutes=_int_or_none(outbound.get("total_duration")),
            duration_return_minutes=_int_or_none(return_option.get("total_duration")),
            segments_outbound=outbound_segments,
            segments_return=return_segments,
            layovers_outbound=outbound_layovers,
            layovers_return=return_layovers,
            baggage_notes=_baggage_notes(outbound, return_option),
            link=_safe_google_link(outbound_payload),
            notes=notes,
        )


def _options(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = payload.get("best_flights") or payload.get("other_flights") or []
    return [item for item in candidates if isinstance(item, dict)] if isinstance(candidates, list) else []


def _segments(values: Any) -> list[FlightSegment]:
    if not isinstance(values, list):
        return []
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        departure = item.get("departure_airport") or {}
        arrival = item.get("arrival_airport") or {}
        if not isinstance(departure, dict) or not isinstance(arrival, dict):
            continue
        origin, destination = departure.get("id"), arrival.get("id")
        if not origin or not destination:
            continue
        result.append(
            FlightSegment(
                origin=str(origin).upper(),
                destination=str(destination).upper(),
                departure_at=datetime_from_api(departure.get("time")),
                arrival_at=datetime_from_api(arrival.get("time")),
                airline=item.get("airline"),
                flight_number=item.get("flight_number"),
            )
        )
    return result


def _layovers(values: Any) -> list[Layover]:
    if not isinstance(values, list):
        return []
    result = []
    for item in values:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        try:
            duration = int(item.get("duration", 0))
        except (ValueError, TypeError):
            continue
        result.append(Layover(airport=str(item["id"]).upper(), duration_minutes=max(duration, 0)))
    return result


def _valid_connections(
    option: dict[str, Any], segments: list[FlightSegment], layovers: list[Layover], config: WatcherConfig
) -> bool:
    connections = config.connections
    if len(segments) - 1 > connections.maximum_stops_per_direction:
        return False
    durations = [layover.duration_minutes for layover in layovers]
    if len(layovers) != len(segments) - 1:
        return False
    if any(
        duration < float(connections.minimum_layover_hours * 60)
        or duration > float(connections.maximum_layover_hours * 60)
        for duration in durations
    ):
        return False
    total = _int_or_none(option.get("total_duration"))
    if total is None or total > float(connections.maximum_direction_duration_hours * 60):
        return False
    for previous, current in pairwise(segments):
        if previous.destination != current.origin:
            return False
        if previous.arrival_at and current.departure_at and current.departure_at <= previous.arrival_at:
            return False
    return True


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None
    return parsed if parsed > 0 else None


def _int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (ValueError, TypeError):
        return None
    return parsed if parsed >= 0 else None


def _baggage_notes(*options: dict[str, Any]) -> list[str]:
    notes = []
    for option in options:
        notes.extend(note for note in option.get("extensions", []) if "baggage" in str(note).lower() or "bag" in str(note).lower())
    return list(dict.fromkeys(notes))


def _safe_google_link(payload: dict[str, Any]) -> str | None:
    value = payload.get("search_metadata", {}).get("google_flights_url")
    return value if isinstance(value, str) and value.startswith("https://www.google.com/travel/flights") else None

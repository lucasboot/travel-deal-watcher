from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

from .models import FareObservation, WatcherConfig


def assess_fare(
    fare: FareObservation, config: WatcherConfig, state: dict[str, Any]
) -> dict[str, Any]:
    reasons: list[str] = []
    issues: list[str] = []
    price_per_person = fare.price_per_person
    duration = (fare.return_date - fare.departure_date).days
    if fare.currency != config.app.currency:
        issues.append(f"currency_mismatch:{fare.currency}")
    if not config.trip.duration_min_days <= duration <= config.trip.duration_max_days:
        issues.append("trip_duration_out_of_range")
    if fare.confidence == "cached_indicative":
        reasons.append("cached_price_is_indicative")
    if fare.confidence == "recent_query":
        reasons.append("price_seen_in_recent_query")

    details_complete = bool(fare.segments_outbound and fare.segments_return)
    if not details_complete:
        issues.append("itinerary_details_missing")
    if details_complete:
        if not _leg_is_coherent(fare.segments_outbound, fare.origin, fare.destination):
            issues.append("outbound_route_incoherent")
        if not _leg_is_coherent(fare.segments_return, fare.destination, fare.origin):
            issues.append("return_route_incoherent")
        for label, segments, layovers, duration_minutes in (
            ("outbound", fare.segments_outbound, fare.layovers_outbound, fare.duration_outbound_minutes),
            ("return", fare.segments_return, fare.layovers_return, fare.duration_return_minutes),
        ):
            count = len(segments) - 1
            if count > config.connections.maximum_stops_per_direction:
                issues.append(f"{label}_too_many_stops")
            if count > 0 and len(layovers) != count:
                issues.append(f"{label}_layover_data_missing")
            for layover in layovers:
                hours = Decimal(layover.duration_minutes) / Decimal(60)
                if hours < config.connections.minimum_layover_hours:
                    issues.append(f"{label}_layover_too_short")
                if hours > config.connections.maximum_layover_hours:
                    issues.append(f"{label}_layover_too_long")
            if duration_minutes is None:
                issues.append(f"{label}_total_duration_missing")
            elif Decimal(duration_minutes) > config.connections.maximum_direction_duration_hours * 60:
                issues.append(f"{label}_total_duration_too_long")
        if fare.layovers_outbound or fare.layovers_return:
            reasons.append("connection_details_checked")
        else:
            reasons.append("no_connections_reported")
    else:
        # Cached providers may report counts, but cannot show the connection windows.
        if fare.transfers_outbound is not None or fare.transfers_return is not None:
            reasons.append("connection_count_known_but_times_unverified")

    if price_per_person <= config.alerts.interesting_price_brl_per_person:
        reasons.append("under_interesting_price_threshold")
    if fare.baggage_notes:
        reasons.append("baggage_conditions_available")
    else:
        reasons.append("baggage_cost_unknown")

    eligible = (
        not issues
        and details_complete
        and fare.confidence == "recent_query"
        and fare.currency == config.app.currency
        and price_per_person <= config.alerts.interesting_price_brl_per_person
    )
    previous = state["last_alert_prices"].get(fare.observation_id)
    if previous:
        try:
            prior_price = Decimal(str(previous["price_per_person"]))
            drop_value = prior_price - price_per_person
            drop_percent = (drop_value / prior_price * Decimal(100)) if prior_price else Decimal(0)
            eligible = eligible and (
                drop_percent >= config.alerts.re_alert_drop_percent
                and drop_value >= config.alerts.re_alert_drop_brl_per_person
            )
            if eligible:
                reasons.append(f"price_drop_{drop_percent:.1f}_percent")
        except (KeyError, ArithmeticError, ValueError):
            pass
    return {
        "eligible_for_immediate_alert": eligible,
        "price_per_person": str(price_per_person),
        "price_threshold": str(config.alerts.interesting_price_brl_per_person),
        "currency": fare.currency,
        "trip_days": duration,
        "reasons": reasons,
        "issues": issues,
        "evaluated_at": datetime.now(UTC).isoformat(),
    }


def _leg_is_coherent(segments, expected_origin: str, expected_destination: str) -> bool:
    if not segments or segments[0].origin != expected_origin or segments[-1].destination != expected_destination:
        return False
    for first, following in pairwise(segments):
        if first.destination != following.origin:
            return False
        if first.arrival_at and following.departure_at and following.departure_at <= first.arrival_at:
            return False
    return not (
        segments[0].departure_at
        and segments[-1].arrival_at
        and segments[0].departure_at.tzinfo is not None
        and segments[-1].arrival_at.tzinfo is not None
        and segments[-1].arrival_at <= segments[0].departure_at
    )


def history_summary(fare: FareObservation, state: dict[str, Any]) -> dict[str, Any]:
    by_day: dict[str, Decimal] = {}
    cutoff = datetime.now(UTC).date() - timedelta(days=30)
    for item in state["fares"].values():
        if (
            item.get("origin") == fare.origin
            and item.get("destination") == fare.destination
            and item.get("departure_date") == fare.departure_date.isoformat()
            and item.get("return_date") == fare.return_date.isoformat()
            and item.get("currency") == fare.currency
            and item.get("passengers") == fare.passengers
            and item.get("cabin_class") == fare.cabin_class
        ):
            try:
                observed_at = datetime.fromisoformat(item["observed_at"])
                if observed_at.date() < cutoff:
                    continue
                day = observed_at.date().isoformat()
                price = Decimal(str(item["total_price"])) / fare.passengers
                by_day[day] = min(by_day.get(day, price), price)
            except (KeyError, ArithmeticError, ValueError):
                continue
    observations = list(by_day.values())
    if len(observations) < 7:
        return {"comparable_observations": len(observations), "median_price_per_person": None}
    ordered = sorted(observations)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {
        "comparable_observations": len(observations),
        "median_price_per_person": str(median),
        "below_median": fare.price_per_person < median,
    }

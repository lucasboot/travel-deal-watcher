from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from travel_deal_watcher.evaluation import assess_fare, history_summary
from travel_deal_watcher.models import FareObservation, FlightSegment, Layover
from travel_deal_watcher.planner import (
    PlannedQuery,
    mark_query_result,
    reserve_query,
    serapi_queries_used,
)
from travel_deal_watcher.state import StateStore, empty_state


def sample_fare(config, *, layover_minutes=180):
    return FareObservation(
        observation_id="test-route",
        source="test",
        confidence="recent_query",
        observed_at=datetime.now(UTC),
        origin="NAT",
        destination="PEK",
        departure_date=date(2027, 2, 3),
        return_date=date(2027, 2, 24),
        total_price=Decimal(7800),
        currency="BRL",
        passengers=1,
        cabin_class="economy",
        transfers_outbound=1,
        transfers_return=0,
        duration_outbound_minutes=1380,
        duration_return_minutes=1320,
        segments_outbound=[
            FlightSegment(origin="NAT", destination="LIS", departure_at="2027-02-03T10:00:00Z", arrival_at="2027-02-03T20:00:00Z"),
            FlightSegment(origin="LIS", destination="PEK", departure_at="2027-02-04T00:00:00Z", arrival_at="2027-02-04T18:00:00Z"),
        ],
        segments_return=[
            FlightSegment(origin="PEK", destination="NAT", departure_at="2027-02-24T15:00:00Z", arrival_at="2027-02-25T12:00:00Z")
        ],
        layovers_outbound=[Layover(airport="LIS", duration_minutes=layover_minutes)],
    )


def test_unverified_cache_never_qualifies_for_immediate_alert(config):
    fare = sample_fare(config)
    fare.confidence = "cached_indicative"
    result = assess_fare(fare, config, empty_state())
    assert not result["eligible_for_immediate_alert"]
    assert "cached_price_is_indicative" in result["reasons"]


def test_complete_fare_under_threshold_can_alert(config):
    result = assess_fare(sample_fare(config), config, empty_state())
    assert result["eligible_for_immediate_alert"]
    assert result["price_per_person"] == "7800"


def test_tight_connection_is_rejected(config):
    result = assess_fare(sample_fare(config, layover_minutes=75), config, empty_state())
    assert not result["eligible_for_immediate_alert"]
    assert "outbound_layover_too_short" in result["issues"]


def test_history_requires_seven_distinct_days(config):
    fare = sample_fare(config)
    state = empty_state()
    for offset in range(6):
        observed = (datetime.now(UTC) - timedelta(days=offset)).isoformat()
        state["fares"][str(offset)] = {
            "origin": fare.origin,
            "destination": fare.destination,
            "departure_date": fare.departure_date.isoformat(),
            "return_date": fare.return_date.isoformat(),
            "currency": fare.currency,
            "passengers": fare.passengers,
            "cabin_class": fare.cabin_class,
            "observed_at": observed,
            "total_price": "8000",
        }
    assert history_summary(fare, state)["median_price_per_person"] is None


def test_query_budget_and_cache_are_enforced():
    state = empty_state()
    query = PlannedQuery("serpapi", "NAT", "PEK", "2027-02-01", "2027-02-28", 21, "calendar")
    allowed, reason = reserve_query(state, query, 1)
    assert allowed and reason == "reserved"
    assert serapi_queries_used(state) == 1
    assert reserve_query(state, query, 1) == (False, "cached")
    next_query = PlannedQuery("serpapi", "REC", "PEK", "2027-02-01", "2027-02-28", 21, "calendar")
    assert reserve_query(state, next_query, 1) == (False, "monthly_budget_exhausted")
    mark_query_result(state, query, "success")


def test_state_store_saves_atomically_and_rejects_corruption(tmp_path):
    path = tmp_path / "nested" / "state.json"
    store = StateStore(path)
    with store.locked() as state:
        state["runs"]["test"] = datetime.now(UTC).isoformat()
    assert store.load()["runs"]["test"]
    path.write_text("{")
    with pytest.raises(RuntimeError, match="refusing to reset"):
        store.load()

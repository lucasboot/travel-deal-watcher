from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

from travel_deal_watcher.planner import PlannedQuery
from travel_deal_watcher.providers.serpapi import SerpApiProvider
from travel_deal_watcher.providers.travelpayouts import TravelpayoutsProvider


def test_travelpayouts_normalizes_only_round_trips_in_duration(config, monkeypatch):
    provider = TravelpayoutsProvider(token="test-token")

    async def response(self, url, *, params=None, headers=None):
        assert params["one_way"] == "false"
        assert params["currency"] == "brl"
        return {
            "success": True,
            "currency": "brl",
            "data": [
                {
                    "origin": "NAT",
                    "destination": "PEK",
                    "departure_at": "2027-02-03T10:00:00Z",
                    "return_at": "2027-02-24T12:00:00Z",
                    "price": 7600,
                    "transfers": 1,
                    "return_transfers": 1,
                    "duration": 1200,
                    "link": "/search/NATPEK",
                },
                {
                    "origin": "NAT",
                    "destination": "PEK",
                    "departure_at": "2027-02-03T10:00:00Z",
                    "return_at": "2027-02-10T12:00:00Z",
                    "price": 4100,
                },
            ],
        }

    monkeypatch.setattr(TravelpayoutsProvider, "get_json", response)
    query = PlannedQuery("travelpayouts", "NAT", "PEK", "2027-02", "2027-02")
    fares = asyncio.run(provider.search(query, config))
    assert len(fares) == 1
    assert fares[0].confidence == "cached_indicative"
    assert fares[0].total_price == Decimal(7600)
    assert fares[0].segments_outbound == []


def test_travelpayouts_skips_prices_for_unverified_passenger_mix(config, monkeypatch):
    provider = TravelpayoutsProvider(token="test-token")

    async def unexpected_request(*args, **kwargs):
        raise AssertionError("passenger-specific fare must not be queried from a source without that filter")

    monkeypatch.setattr(TravelpayoutsProvider, "get_json", unexpected_request)
    multi_passenger_config = config.model_copy(update={"trip": config.trip.model_copy(update={"adults": 2})})
    query = PlannedQuery("travelpayouts", "NAT", "PEK", "2027-02", "2027-02")
    assert asyncio.run(provider.search(query, multi_passenger_config)) == []


def _flight(origin, destination, departure, arrival, number):
    return {
        "departure_airport": {"id": origin, "time": departure},
        "arrival_airport": {"id": destination, "time": arrival},
        "airline": "Test Air",
        "flight_number": number,
    }


def test_serpapi_requires_complete_coherent_round_trip(config):
    provider = SerpApiProvider(api_key="test-key")
    outbound = {
        "search_metadata": {"google_flights_url": "https://www.google.com/travel/flights?test=1"},
        "best_flights": [
            {
                "flights": [_flight("NAT", "PEK", "2027-02-03 10:00", "2027-02-04 09:00", "TA1")],
                "total_duration": 1380,
                "layovers": [],
                "departure_token": "opaque-token",
                "price": 7900,
            }
        ],
    }
    returning = {
        "best_flights": [
            {
                "flights": [_flight("PEK", "NAT", "2027-02-24 15:00", "2027-02-25 12:00", "TA2")],
                "total_duration": 1380,
                "layovers": [],
            }
        ]
    }
    fare = provider.build_observation(
        outbound_payload=outbound,
        return_payload=returning,
        origin="NAT",
        destination="PEK",
        departure="2027-02-03",
        returned="2027-02-24",
        calendar_price=Decimal(7900),
        config=config,
    )
    assert fare is not None
    assert fare.total_price == Decimal(7900)
    assert fare.departure_date == date(2027, 2, 3)
    assert fare.confidence == "recent_query"


def test_serpapi_search_requests_respect_route_filters(config, monkeypatch):
    provider = SerpApiProvider(api_key="test-key")
    calls = []

    async def response(self, url, *, params=None, headers=None):
        calls.append(params)
        return {}

    monkeypatch.setattr(SerpApiProvider, "get_json", response)
    asyncio.run(
        provider.search_outbound(
            origin="NAT",
            destination="PEK",
            departure="2027-02-03",
            returned="2027-02-24",
            config=config,
        )
    )
    asyncio.run(provider.search_return("opaque-token"))
    assert calls[0]["stops"] == 3
    assert calls[0]["layover_duration"] == "120,360"
    assert calls[0]["max_duration"] == 2160
    assert calls[0]["no_cache"] == "true"
    assert calls[1]["departure_token"] == "opaque-token"
    assert calls[1]["no_cache"] == "true"


def test_serpapi_rejects_wrong_return_airport(config):
    provider = SerpApiProvider(api_key="test-key")
    outbound = {
        "best_flights": [
            {
                "flights": [_flight("NAT", "PEK", "2027-02-03 10:00", "2027-02-04 09:00", "TA1")],
                "total_duration": 1380,
                "layovers": [],
                "departure_token": "opaque-token",
            }
        ]
    }
    returning = {
        "best_flights": [
            {
                "flights": [_flight("CAN", "NAT", "2027-02-24 15:00", "2027-02-25 12:00", "TA2")],
                "total_duration": 1380,
                "layovers": [],
            }
        ]
    }
    assert provider.build_observation(
        outbound_payload=outbound,
        return_payload=returning,
        origin="NAT",
        destination="PEK",
        departure="2027-02-03",
        returned="2027-02-24",
        calendar_price=Decimal(7900),
        config=config,
    ) is None


def test_serpapi_account_must_be_free(config, monkeypatch):
    provider = SerpApiProvider(api_key="test-key")

    async def paid(self, url, *, params=None, headers=None):
        return {"plan_monthly_price": 25, "plan_searches_left": 250}

    monkeypatch.setattr(SerpApiProvider, "get_json", paid)
    allowed, reason, remaining = asyncio.run(provider.free_quota_available(200))
    assert not allowed
    assert "zero-cost" in reason
    assert remaining == 0


def test_serpapi_calendar_price_without_detailed_fare_is_indicative(config):
    provider = SerpApiProvider(api_key="test-key")
    outbound = {
        "best_flights": [
            {
                "flights": [_flight("NAT", "PEK", "2027-02-03 10:00", "2027-02-04 09:00", "TA1")],
                "total_duration": 1380,
                "layovers": [],
                "departure_token": "opaque-token",
            }
        ]
    }
    returning = {
        "best_flights": [
            {
                "flights": [_flight("PEK", "NAT", "2027-02-24 15:00", "2027-02-25 12:00", "TA2")],
                "total_duration": 1380,
                "layovers": [],
            }
        ]
    }
    fare = provider.build_observation(
        outbound_payload=outbound,
        return_payload=returning,
        origin="NAT",
        destination="PEK",
        departure="2027-02-03",
        returned="2027-02-24",
        calendar_price=Decimal(7900),
        config=config,
    )
    assert fare is not None
    assert fare.confidence == "cached_indicative"
    assert fare.total_price == Decimal(7900)
    assert any("calendário em cache" in note for note in fare.notes)

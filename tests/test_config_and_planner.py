from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from travel_deal_watcher.config import load_config
from travel_deal_watcher.locations import airport_pairs
from travel_deal_watcher.planner import (
    candidate_month_pairs,
    month_string,
    plan_serpapi,
    plan_travelpayouts,
)
from travel_deal_watcher.state import empty_state


def test_initial_routes_exclude_repositioning_airports(config):
    assert airport_pairs(config)[:2] == [("NAT", "PEK"), ("NAT", "PKX")]
    assert all(origin not in {"GRU", "GIG"} for origin, _ in airport_pairs(config))


def test_month_planning_rolls_over_new_year(config):
    assert month_string(3, date(2026, 11, 16)) == "2027-02"
    months = candidate_month_pairs(config, date(2026, 11, 16))
    assert ("2027-02", "2027-02") in months


def test_planner_rotates_and_respects_query_caps(config):
    state = empty_state()
    first = plan_travelpayouts(config, state, date(2026, 11, 16))
    second = plan_travelpayouts(config, state, date(2026, 11, 16))
    assert len(first) == config.schedule.travelpayouts_max_queries_per_cycle
    assert len(second) == len(first)
    assert first[0].key != second[0].key
    calendar = plan_serpapi(config, empty_state(), date(2026, 11, 16))
    assert len(calendar) == 2
    assert all(item.query_type == "calendar" for item in calendar)


def test_config_environment_overrides_and_empty_variables(config, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text((__import__("pathlib").Path(__file__).resolve().parents[1] / "config.yaml").read_text())
    loaded = load_config(
        path,
        environ={
            "WATCHER_INTERESTING_PRICE_BRL": "7500",
            "WATCHER_SERPAPI_ENABLED": "false",
            "WATCHER_SERPAPI_MONTHLY_BUDGET": "",
        },
    )
    assert str(loaded.alerts.interesting_price_brl_per_person) == "7500"
    assert loaded.sources["serpapi"].enabled is False
    assert loaded.schedule.serpapi_max_queries_per_month == 200


def test_invalid_trip_configuration_is_rejected(config):
    data = config.model_dump()
    data["trip"]["months_ahead_min"] = 12
    data["trip"]["months_ahead_max"] = 3
    with pytest.raises(ValidationError):
        type(config).model_validate(data)

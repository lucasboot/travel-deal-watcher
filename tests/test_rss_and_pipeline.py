from __future__ import annotations

import asyncio

from travel_deal_watcher.pipeline import run
from travel_deal_watcher.providers.rss import destination_aliases, extract_price, match_destination
from travel_deal_watcher.state import StateStore


def test_rss_filters_china_without_matching_origin_natal(config):
    aliases = destination_aliases(config)
    assert match_destination("Natal tem promoção de hospedagem", aliases) is None
    assert match_destination("Passagens para Xangai e Pequim", aliases) in {"xangai", "pequim"}


def test_rss_price_parser_supports_brazilian_format():
    price, currency = extract_price("Passagens para Pequim por R$ 7.899,90")
    assert str(price) == "7899.90"
    assert currency == "BRL"


def test_source_check_requires_explicit_terms_and_secrets(config, tmp_path, monkeypatch):
    monkeypatch.delenv("TRAVELPAYOUTS_TOKEN", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    store = StateStore(tmp_path / "state.json")
    result = asyncio.run(run(config, store, "source-check"))
    state = store.load()
    assert result["queries"] == 0
    assert state["validation"]["travelpayouts"]["passed"] is False
    assert state["validation"]["serpapi"]["passed"] is False
    assert "terms" in state["validation"]["serpapi"]["reason"]

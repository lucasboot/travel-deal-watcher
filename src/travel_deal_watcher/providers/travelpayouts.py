from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..models import FareObservation, WatcherConfig
from ..planner import PlannedQuery
from .base import HttpProvider, ProviderConfigurationError, datetime_from_api, iso_date

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


class TravelpayoutsProvider(HttpProvider):
    def __init__(self, token: str | None = None) -> None:
        super().__init__(name="travelpayouts")
        self.token = (token if token is not None else os.getenv("TRAVELPAYOUTS_TOKEN", "")).strip()
        if not self.token:
            raise ProviderConfigurationError("TRAVELPAYOUTS_TOKEN is not configured")

    async def search(self, query: PlannedQuery, config: WatcherConfig) -> list[FareObservation]:
        if config.trip.adults != 1 or config.trip.children or config.trip.infants:
            LOGGER.info(
                "provider=travelpayouts skipped: cached fare data does not support the configured passenger mix"
            )
            return []
        data = await self.get_json(
            BASE_URL,
            params={
                "origin": query.origin,
                "destination": query.destination,
                "departure_at": query.departure_month,
                "return_at": query.return_month,
                "one_way": "false",
                "currency": config.app.currency.lower(),
                "market": config.app.locale_country,
                "sorting": "price",
                "limit": 30,
                "token": self.token,
            },
        )
        if data.get("success") is False:
            LOGGER.info("provider=travelpayouts query returned no cached data")
            return []
        rows = data.get("data", [])
        if isinstance(rows, dict):
            rows = list(rows.values())
        if not isinstance(rows, list):
            raise TypeError("Travelpayouts returned an unexpected data structure")
        observations = []
        now = datetime.now(UTC)
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                price = Decimal(str(row.get("price", row.get("value"))))
            except (InvalidOperation, TypeError):
                continue
            departure = iso_date(row.get("departure_at"))
            returned = iso_date(row.get("return_at"))
            if not departure or not returned or returned < departure or price <= 0:
                continue
            # Keep only combinations consistent with the configured trip length.
            duration = (returned - departure).days
            if not config.trip.duration_min_days <= duration <= config.trip.duration_max_days:
                continue
            found_at = datetime_from_api(row.get("found_at"))
            transfer_count = _int_or_none(row.get("transfers"))
            return_transfer_count = _int_or_none(row.get("return_transfers"))
            link_path = row.get("link")
            link = f"https://www.aviasales.com{link_path}" if isinstance(link_path, str) and link_path.startswith("/") else None
            observations.append(
                FareObservation(
                    observation_id=_observation_id(
                        query.origin, query.destination, departure.isoformat(), returned.isoformat(), price
                    ),
                    source="travelpayouts_data_api",
                    confidence="cached_indicative",
                    observed_at=now,
                    source_found_at=found_at,
                    origin=str(row.get("origin_airport") or row.get("origin") or query.origin).upper(),
                    destination=str(row.get("destination_airport") or row.get("destination") or query.destination).upper(),
                    departure_date=departure,
                    return_date=returned,
                    total_price=price,
                    currency=str(row.get("currency") or data.get("currency") or config.app.currency).upper(),
                    passengers=config.trip.adults + config.trip.children + config.trip.infants,
                    cabin_class=config.trip.cabin_class,
                    transfers_outbound=transfer_count,
                    transfers_return=return_transfer_count,
                    duration_outbound_minutes=_int_or_none(row.get("duration")),
                    link=link,
                    notes=["Preço indicativo obtido de cache; verificar antes da compra."],
                )
            )
        return observations


def _int_or_none(value: Any) -> int | None:
    try:
        result = int(value)
    except (ValueError, TypeError):
        return None
    return result if result >= 0 else None


def _observation_id(origin: str, destination: str, departure: str, returned: str, price: Decimal) -> str:
    return f"tp:{origin}:{destination}:{departure}:{returned}:{price}"

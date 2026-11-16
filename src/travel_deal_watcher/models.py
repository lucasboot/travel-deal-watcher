from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Origin(StrictModel):
    code: str
    role: Literal["primary", "alternative", "repositioning"] = "alternative"

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        code = value.strip().upper()
        if len(code) != 3 or not code.isalpha():
            raise ValueError("Airport codes must contain three letters")
        return code


class Destination(StrictModel):
    name: str
    kind: Literal["country", "city", "airport"]
    code: str
    airports: list[str] = Field(default_factory=list)
    match_keywords: list[str] = Field(default_factory=list)

    @field_validator("code")
    @classmethod
    def normalize_destination_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("Destination code or name cannot be empty")
        return normalized

    @field_validator("airports")
    @classmethod
    def normalize_airports(cls, values: list[str]) -> list[str]:
        result = []
        for value in values:
            code = value.strip().upper()
            if len(code) != 3 or not code.isalpha():
                raise ValueError(f"Invalid destination airport code: {value}")
            if code not in result:
                result.append(code)
        return result


class TripConfig(StrictModel):
    months_ahead_min: int = Field(default=3, ge=1, le=24)
    months_ahead_max: int = Field(default=10, ge=1, le=24)
    duration_min_days: int = Field(default=14, ge=1, le=180)
    duration_max_days: int = Field(default=30, ge=1, le=180)
    durations_to_sample_days: list[int] = Field(default_factory=lambda: [14, 21, 30])
    adults: int = Field(default=1, ge=1, le=9)
    children: int = Field(default=0, ge=0, le=8)
    infants: int = Field(default=0, ge=0, le=8)
    cabin_class: Literal["economy", "premium_economy", "business", "first"] = "economy"

    @model_validator(mode="after")
    def validate_ranges(self) -> TripConfig:
        if self.months_ahead_min > self.months_ahead_max:
            raise ValueError("months_ahead_min must be <= months_ahead_max")
        if self.duration_min_days > self.duration_max_days:
            raise ValueError("duration_min_days must be <= duration_max_days")
        if not self.durations_to_sample_days:
            raise ValueError("At least one sampled trip duration is required")
        if any(not self.duration_min_days <= days <= self.duration_max_days for days in self.durations_to_sample_days):
            raise ValueError("Sampled durations must be within the configured trip range")
        if self.infants > self.adults:
            raise ValueError("There cannot be more infants than adults")
        return self


class ConnectionConfig(StrictModel):
    minimum_layover_hours: Decimal = Field(default=Decimal(2), ge=0)
    maximum_layover_hours: Decimal = Field(default=Decimal(6), gt=0)
    maximum_stops_per_direction: int = Field(default=2, ge=0, le=5)
    maximum_direction_duration_hours: Decimal = Field(default=Decimal(36), gt=0)
    allow_airport_changes: bool = False
    separate_tickets_enabled: bool = False

    @model_validator(mode="after")
    def validate_layover_range(self) -> ConnectionConfig:
        if self.minimum_layover_hours > self.maximum_layover_hours:
            raise ValueError("minimum_layover_hours must be <= maximum_layover_hours")
        return self


class AlertConfig(StrictModel):
    interesting_price_brl_per_person: Decimal = Field(default=Decimal(8000), gt=0)
    reference_price_brl_per_person: Decimal = Field(default=Decimal(10000), gt=0)
    re_alert_drop_percent: Decimal = Field(default=Decimal(5), gt=0, le=100)
    re_alert_drop_brl_per_person: Decimal = Field(default=Decimal(300), ge=0)


class ScheduleConfig(StrictModel):
    promotions_interval_minutes: int = Field(default=30, ge=5)
    discovery_interval_hours: int = Field(default=2, ge=1)
    serpapi_cycles_per_day: int = Field(default=4, ge=1, le=24)
    serpapi_max_queries_per_month: int = Field(default=200, ge=1, le=250)
    travelpayouts_max_queries_per_cycle: int = Field(default=8, ge=1, le=100)
    history_days: int = Field(default=400, ge=30, le=2000)
    daily_digest_hour_fortaleza: int = Field(default=8, ge=0, le=23)


class SourceConfig(StrictModel):
    enabled: bool = True
    terms_accepted: bool = False


class StorageConfig(StrictModel):
    state_path: str = "data/state.json"


class TelegramConfig(StrictModel):
    enabled: bool = True


class AppConfig(StrictModel):
    currency: str = "BRL"
    locale_country: str = "br"
    locale_language: str = "pt"

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        code = value.strip().upper()
        if len(code) != 3 or not code.isalpha():
            raise ValueError("currency must be an ISO-style three-letter code")
        return code


class WatcherConfig(StrictModel):
    app: AppConfig = Field(default_factory=AppConfig)
    origins: list[Origin]
    destinations: list[Destination]
    trip: TripConfig = Field(default_factory=TripConfig)
    connections: ConnectionConfig = Field(default_factory=ConnectionConfig)
    intermediate_airports: list[str] = Field(default_factory=list)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    sources: dict[str, SourceConfig]
    storage: StorageConfig = Field(default_factory=StorageConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)

    @field_validator("intermediate_airports")
    @classmethod
    def validate_intermediates(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper() for value in values]
        if any(len(value) != 3 or not value.isalpha() for value in normalized):
            raise ValueError("Intermediate airport codes must contain three letters")
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def validate_configuration(self) -> WatcherConfig:
        if not self.origins or not self.destinations:
            raise ValueError("At least one origin and one destination are required")
        return self


class FlightSegment(StrictModel):
    origin: str
    destination: str
    departure_at: datetime | None = None
    arrival_at: datetime | None = None
    airline: str | None = None
    flight_number: str | None = None


class Layover(StrictModel):
    airport: str
    duration_minutes: int = Field(ge=0)


class FareObservation(StrictModel):
    observation_id: str
    source: str
    confidence: Literal["cached_indicative", "recent_query"]
    observed_at: datetime
    source_found_at: datetime | None = None
    origin: str
    destination: str
    departure_date: date
    return_date: date
    total_price: Decimal = Field(gt=0)
    currency: str
    passengers: int = Field(ge=1)
    cabin_class: str
    transfers_outbound: int | None = Field(default=None, ge=0)
    transfers_return: int | None = Field(default=None, ge=0)
    duration_outbound_minutes: int | None = Field(default=None, ge=0)
    duration_return_minutes: int | None = Field(default=None, ge=0)
    segments_outbound: list[FlightSegment] = Field(default_factory=list)
    segments_return: list[FlightSegment] = Field(default_factory=list)
    layovers_outbound: list[Layover] = Field(default_factory=list)
    layovers_return: list[Layover] = Field(default_factory=list)
    baggage_notes: list[str] = Field(default_factory=list)
    link: str | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def price_per_person(self) -> Decimal:
        return self.total_price / self.passengers


class PromotionPost(StrictModel):
    promotion_id: str
    source: str
    title: str
    url: str
    published_at: datetime | None = None
    discovered_at: datetime
    destination_match: str | None = None
    mentioned_price: Decimal | None = Field(default=None, gt=0)
    currency: str | None = None

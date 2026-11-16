from __future__ import annotations

from collections.abc import Iterable

from .models import Destination, Origin, WatcherConfig


def airport_pairs(config: WatcherConfig) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for origin in config.origins:
        if origin.role == "repositioning":
            continue
        for destination in config.destinations:
            for airport in destination.airports or ([destination.code] if len(destination.code) == 3 else []):
                pair = (origin.code, airport)
                if pair not in pairs:
                    pairs.append(pair)
    return pairs


def destination_for_airport(config: WatcherConfig, airport: str) -> Destination | None:
    code = airport.upper()
    return next((dest for dest in config.destinations if code in dest.airports or dest.code == code), None)


def candidate_origins(origins: Iterable[Origin], *, include_repositioning: bool = False) -> list[str]:
    return [
        origin.code
        for origin in origins
        if include_repositioning or origin.role != "repositioning"
    ]

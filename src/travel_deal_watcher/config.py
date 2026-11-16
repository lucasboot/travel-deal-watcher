from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .models import WatcherConfig

ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "WATCHER_INTERESTING_PRICE_BRL": ("alerts", "interesting_price_brl_per_person"),
    "WATCHER_PROMOTIONS_INTERVAL_MINUTES": ("schedule", "promotions_interval_minutes"),
    "WATCHER_DISCOVERY_INTERVAL_HOURS": ("schedule", "discovery_interval_hours"),
    "WATCHER_SERPAPI_CYCLES_PER_DAY": ("schedule", "serpapi_cycles_per_day"),
    "WATCHER_SERPAPI_MONTHLY_BUDGET": ("schedule", "serpapi_max_queries_per_month"),
    "WATCHER_TP_MAX_QUERIES_PER_CYCLE": ("schedule", "travelpayouts_max_queries_per_cycle"),
    "WATCHER_STATE_PATH": ("storage", "state_path"),
    "WATCHER_TRAVELPAYOUTS_ENABLED": ("sources", "travelpayouts", "enabled"),
    "WATCHER_SERPAPI_ENABLED": ("sources", "serpapi", "enabled"),
    "WATCHER_RSS_ENABLED": ("sources", "melhores_destinos_rss", "enabled"),
    "WATCHER_TRAVELPAYOUTS_TERMS_ACCEPTED": ("sources", "travelpayouts", "terms_accepted"),
    "WATCHER_SERPAPI_TERMS_ACCEPTED": ("sources", "serpapi", "terms_accepted"),
}


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'").strip())


def _parse_override(raw: str, current: Any) -> Any:
    if isinstance(current, bool):
        normalized = raw.strip().lower()
        if normalized not in {"true", "false", "1", "0", "yes", "no"}:
            raise ValueError(f"Expected a boolean override, received {raw!r}")
        return normalized in {"true", "1", "yes"}
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    return raw


def load_config(path: str | Path = "config.yaml", environ: dict[str, str] | None = None) -> WatcherConfig:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise TypeError("The YAML configuration root must be an object")

    env = os.environ if environ is None else environ
    for env_name, route in ENV_OVERRIDES.items():
        if env_name not in env or not env[env_name].strip():
            continue
        cursor = raw
        for key in route[:-1]:
            cursor = cursor.setdefault(key, {})
        leaf = route[-1]
        previous = cursor.get(leaf, "")
        cursor[leaf] = _parse_override(env[env_name], previous)

    return WatcherConfig.model_validate(raw)

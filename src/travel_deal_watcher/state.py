from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

LOGGER = logging.getLogger(__name__)
STATE_VERSION = 1


def empty_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_VERSION,
        "runs": {},
        "cursors": {},
        "query_log": [],
        "query_cache": {},
        "fares": {},
        "promotions": {},
        "deliveries": {},
        "last_alert_prices": {},
        "validation": {},
    }


class StateStore:
    """Small JSON store with a process lock and atomic replacement."""

    def __init__(self, path: str | Path, timeout_seconds: int = 30) -> None:
        self.path = Path(path)
        self.lock = FileLock(f"{self.path}.lock", timeout=timeout_seconds)

    @contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.lock:
                state = self.load()
                yield state
                self.save(state)
        except Timeout as exc:
            raise RuntimeError(f"Could not lock state file {self.path}") from exc

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return empty_state()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"State file is unreadable; refusing to reset {self.path}") from exc
        if not isinstance(data, dict) or data.get("schema_version") != STATE_VERSION:
            raise RuntimeError(f"Unsupported state format in {self.path}; refusing to overwrite it")
        expected = empty_state()
        for key, default in expected.items():
            if key not in data:
                data[key] = default
            elif not isinstance(data[key], type(default)):
                raise RuntimeError(f"Invalid state field {key!r} in {self.path}")
        return data

    def save(self, state: dict[str, Any]) -> None:
        state["schema_version"] = STATE_VERSION
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as temp_file:
                json.dump(state, temp_file, ensure_ascii=False, indent=2, default=_json_default)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_name = temp_file.name
            os.replace(temp_name, self.path)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)

    def prune(self, state: dict[str, Any], history_days: int) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=history_days)
        for collection in ("fares", "promotions"):
            items = state[collection]
            for item_id, item in list(items.items()):
                timestamp = item.get("observed_at") or item.get("discovered_at")
                if not timestamp:
                    continue
                try:
                    observed = datetime.fromisoformat(timestamp)
                except (TypeError, ValueError):
                    continue
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=UTC)
                if observed < cutoff:
                    del items[item_id]
        log_cutoff = cutoff.isoformat()
        state["query_log"] = [
            entry for entry in state["query_log"] if entry.get("created_at", "") >= log_cutoff
        ]
        for query_key, expiry in list(state["query_cache"].items()):
            if expiry <= datetime.now(UTC).isoformat():
                del state["query_cache"][query_key]


def _json_default(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "as_tuple"):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")

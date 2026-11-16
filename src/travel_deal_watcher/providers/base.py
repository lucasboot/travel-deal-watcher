from __future__ import annotations

import asyncio
import logging
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ProviderError(RuntimeError):
    pass


class ProviderConfigurationError(ProviderError):
    pass


class QuotaExceededError(ProviderError):
    pass


class HttpProvider:
    def __init__(self, *, name: str, timeout: float = 25, retries: int = 3) -> None:
        self.name = name
        self.timeout = timeout
        self.retries = retries

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": "travel-deal-watcher/0.1 (+https://github.com/actions)", **(headers or {})},
            follow_redirects=True,
        ) as client:
            for attempt in range(self.retries):
                try:
                    response = await client.get(url, params=params)
                    if response.status_code < 400:
                        body = response.json()
                        if not isinstance(body, dict):
                            raise ProviderError(f"{self.name} returned a non-object JSON payload")
                        return body
                    if response.status_code not in RETRYABLE_STATUS or attempt == self.retries - 1:
                        response.raise_for_status()
                    retry_after = _retry_after(response.headers.get("Retry-After"))
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                    if attempt == self.retries - 1:
                        raise ProviderError(f"{self.name} network request failed") from exc
                    retry_after = None
                except httpx.HTTPStatusError as exc:
                    raise ProviderError(
                        f"{self.name} returned HTTP {exc.response.status_code}"
                    ) from exc
                except ValueError as exc:
                    raise ProviderError(f"{self.name} returned invalid JSON") from exc
                delay = retry_after if retry_after is not None else 2**attempt + random.random() * 0.25
                LOGGER.warning("provider=%s retry=%s delay_seconds=%.1f", self.name, attempt + 1, delay)
                await asyncio.sleep(min(delay, 60))
        raise ProviderError(f"{self.name} request retries were exhausted")

    async def get_bytes(self, url: str) -> bytes:
        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": "travel-deal-watcher/0.1 (+https://github.com/actions)"},
            follow_redirects=True,
        ) as client:
            for attempt in range(self.retries):
                try:
                    response = await client.get(url)
                    if response.status_code < 400:
                        return response.content
                    if response.status_code not in RETRYABLE_STATUS or attempt == self.retries - 1:
                        response.raise_for_status()
                    retry_after = _retry_after(response.headers.get("Retry-After"))
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                    if attempt == self.retries - 1:
                        raise ProviderError(f"{self.name} network request failed") from exc
                    retry_after = None
                except httpx.HTTPStatusError as exc:
                    raise ProviderError(
                        f"{self.name} returned HTTP {exc.response.status_code}"
                    ) from exc
                delay = retry_after if retry_after is not None else 2**attempt + random.random() * 0.25
                await asyncio.sleep(min(delay, 60))
        raise ProviderError(f"{self.name} request retries were exhausted")


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0, float(value))
    except ValueError:
        try:
            timestamp = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return max(0, (timestamp - datetime.now(UTC)).total_seconds())


def datetime_from_api(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    # Flight segment times are local airport times without offsets; keep them naive.
    return result


def iso_date(value: Any):
    from datetime import date

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None

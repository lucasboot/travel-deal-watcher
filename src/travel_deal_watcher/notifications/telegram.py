from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryResult:
    recipient_key: str
    status: str
    detail: str | None = None


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str | None = None,
        chat_ids: list[str] | None = None,
        *,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.bot_token = (bot_token if bot_token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        raw_chats = os.getenv("TELEGRAM_CHAT_IDS", "")
        self.chat_ids = chat_ids if chat_ids is not None else [item.strip() for item in raw_chats.split(",") if item.strip()]
        hash_key = os.getenv("STATE_HASH_SECRET") or self.bot_token
        self.hash_key = hash_key.encode() if hash_key else b""

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.bot_token and self.chat_ids)

    def recipient_key(self, chat_id: str) -> str:
        if not self.hash_key:
            raise RuntimeError("Set STATE_HASH_SECRET when storing Telegram delivery state")
        digest = hmac.new(self.hash_key, chat_id.encode(), hashlib.sha256).hexdigest()[:20]
        return f"telegram:{digest}"

    @property
    def recipients(self) -> list[tuple[str, str]]:
        return [(chat_id, self.recipient_key(chat_id)) for chat_id in self.chat_ids]

    async def send_to(self, chat_id: str, message: str) -> DeliveryResult:
        if not self.bot_token:
            print(message)
            return DeliveryResult("preview", "preview")
        recipient = self.recipient_key(chat_id)
        if len(message) > 4000:
            message = message[:3970] + "\n… mensagem truncada"
        async with httpx.AsyncClient(timeout=20) as client:
            return await self._send_one(client, chat_id, recipient, message)

    async def send(self, message: str) -> list[DeliveryResult]:
        if not self.configured:
            print(message)
            return []
        results: list[DeliveryResult] = []
        for chat_id in self.chat_ids:
            results.append(await self.send_to(chat_id, message))
            if len(self.chat_ids) > 1:
                await asyncio.sleep(0.05)
        return results

    async def _send_one(
        self, client: httpx.AsyncClient, chat_id: str, recipient: str, message: str
    ) -> DeliveryResult:
        if len(message) > 4000:
            message = message[:3970] + "\n… mensagem truncada"
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            response = await client.post(
                url,
                json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            # The request might have reached Telegram; do not retry an ambiguous delivery.
            return DeliveryResult(recipient, "unknown", "network result is uncertain")
        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            payload = {}
        if response.status_code == 200 and payload.get("ok") is True:
            return DeliveryResult(recipient, "sent")
        if response.status_code == 429:
            return DeliveryResult(recipient, "failed", "Telegram rate limited this recipient")
        if response.status_code >= 500:
            return DeliveryResult(recipient, "unknown", f"Telegram HTTP {response.status_code}")
        LOGGER.warning("telegram delivery rejected for pseudonymous recipient=%s", recipient)
        return DeliveryResult(recipient, "failed", f"Telegram HTTP {response.status_code}")

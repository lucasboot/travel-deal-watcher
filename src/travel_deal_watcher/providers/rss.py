from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from xml.etree import ElementTree

from ..models import PromotionPost, WatcherConfig
from .base import HttpProvider

LOGGER = logging.getLogger(__name__)
FEED_URL = "https://feeds.feedburner.com/passagens-aereas"
CHINA_ALIASES = {
    "china",
    "chinese",
    "pequim",
    "beijing",
    "xangai",
    "shanghai",
    "guangzhou",
    "cantao",
    "chengdu",
    "changdu",
    "hong kong",
    "asia",
    "ásia",
}


class MelhoresDestinosRSS(HttpProvider):
    def __init__(self) -> None:
        super().__init__(name="melhores_destinos_rss")

    async def fetch(self, config: WatcherConfig) -> list[PromotionPost]:
        content = await self.get_bytes(FEED_URL)
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as exc:
            raise ValueError("Melhores Destinos returned invalid RSS XML") from exc
        items = root.findall("./channel/item")
        aliases = destination_aliases(config)
        now = datetime.now(UTC)
        posts = []
        for item in items:
            title = _element_text(item, "title")
            url = _element_text(item, "link")
            if not title or not url or not _valid_feed_url(url):
                continue
            matched = match_destination(title, aliases)
            if not matched:
                continue
            published = _parse_published(_element_text(item, "pubDate"))
            price, currency = extract_price(title)
            digest = hashlib.sha256(f"melhores_destinos_rss:{url}".encode()).hexdigest()
            posts.append(
                PromotionPost(
                    promotion_id=f"md:{digest}",
                    source="melhores_destinos_rss",
                    title=title[:400],
                    url=url,
                    published_at=published,
                    discovered_at=now,
                    destination_match=matched,
                    mentioned_price=price,
                    currency=currency,
                )
            )
        return posts


def destination_aliases(config: WatcherConfig) -> list[str]:
    aliases: set[str] = set()
    for destination in config.destinations:
        aliases.add(destination.name.lower())
        if destination.kind in {"city", "airport"}:
            aliases.add(destination.code.lower())
        aliases.update(value.lower().strip() for value in destination.match_keywords if value.strip())
        aliases.update(airport.lower() for airport in destination.airports)
        if destination.code == "CN":
            aliases.update(CHINA_ALIASES)
    return sorted(aliases, key=len, reverse=True)


def match_destination(title: str, aliases: list[str]) -> str | None:
    normalized = title.casefold()
    for alias in aliases:
        if len(alias) == 3 and alias.isalpha():
            pattern = rf"(?<![a-z]){re.escape(alias)}(?![a-z])"
            if re.search(pattern, normalized):
                return alias.upper()
        elif alias and alias in normalized:
            return alias
    return None


def extract_price(title: str) -> tuple[Decimal | None, str | None]:
    match = re.search(r"(?:R\$\s*|BRL\s*)([\d.]+(?:,\d{2})?)", title, re.IGNORECASE)
    if not match:
        return None, None
    try:
        price = Decimal(match.group(1).replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None, None
    return (price, "BRL") if price > 0 else (None, None)


def _parse_published(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _element_text(item: ElementTree.Element, tag: str) -> str | None:
    element = item.find(tag)
    return element.text.strip() if element is not None and element.text else None


def _valid_feed_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == "www.melhoresdestinos.com.br"

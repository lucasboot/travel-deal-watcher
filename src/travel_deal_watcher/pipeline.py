from __future__ import annotations

import copy
import logging
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from .evaluation import assess_fare, history_summary
from .models import FareObservation, PromotionPost, WatcherConfig
from .notifications.formatter import format_daily_digest, format_fare_alert, format_promotion
from .notifications.telegram import DeliveryResult, TelegramNotifier
from .planner import (
    PlannedQuery,
    is_due,
    mark_query_result,
    plan_details_query,
    plan_serpapi,
    plan_travelpayouts,
    record_run,
    reserve_query,
    serapi_queries_used,
)
from .providers.base import ProviderConfigurationError
from .providers.rss import MelhoresDestinosRSS
from .providers.serpapi import SerpApiProvider
from .providers.travelpayouts import TravelpayoutsProvider
from .state import StateStore

LOGGER = logging.getLogger(__name__)
FARE_MODES = {"due", "all", "discovery", "fares"}


async def make_plan(config: WatcherConfig, store: StateStore) -> dict[str, Any]:
    state = copy.deepcopy(store.load())
    return {
        "travelpayouts": plan_travelpayouts(config, state),
        "serpapi": plan_serpapi(config, state),
        "serpapi_used_this_month": serapi_queries_used(store.load()),
        "serpapi_monthly_cap": config.schedule.serpapi_max_queries_per_month,
    }


async def run(config: WatcherConfig, store: StateStore, mode: str = "due") -> dict[str, int]:
    if mode not in {"due", "all", "promotions", "discovery", "fares", "digest", "source-check"}:
        raise ValueError(f"Unsupported run mode: {mode}")
    counts = {"queries": 0, "fares": 0, "promotions": 0, "alerts": 0, "errors": 0}
    notifier = TelegramNotifier(enabled=config.telegram.enabled)
    with store.locked() as state:
        store.prune(state, config.schedule.history_days)
        now = datetime.now(UTC)
        force = mode == "all"

        if mode == "source-check":
            await _run_source_check(config, state, store, counts)
            record_run(state, "source_check", now)
            store.save(state)
            return counts

        if mode in {"due", "all", "promotions"}:
            interval = timedelta(minutes=config.schedule.promotions_interval_minutes)
            if force or mode == "promotions" or (mode == "due" and is_due(state, "promotions", interval, now)):
                try:
                    await _run_promotions(config, state, store, notifier, counts)
                except Exception:
                    counts["errors"] += 1
                    LOGGER.exception("provider=melhores_destinos_rss run failed")
                record_run(state, "promotions", now)
                store.save(state)

        if mode in {"due", "all", "discovery"}:
            interval = timedelta(hours=config.schedule.discovery_interval_hours)
            if force or mode == "discovery" or (mode == "due" and is_due(state, "travelpayouts", interval, now)):
                await _run_travelpayouts(config, state, store, notifier, counts)
                record_run(state, "travelpayouts", now)
                store.save(state)

        if mode in {"due", "all", "fares"}:
            interval = timedelta(hours=24 / config.schedule.serpapi_cycles_per_day)
            if force or mode == "fares" or (mode == "due" and is_due(state, "serpapi", interval, now)):
                await _run_serpapi(config, state, store, notifier, counts)
                record_run(state, "serpapi", now)
                store.save(state)

        if mode in {"due", "all", "digest"} and _digest_is_due(config, state, now, force or mode == "digest"):
            await _run_digest(config, state, store, notifier, counts, now)
            record_run(state, "daily_digest", now)
            store.save(state)

    return counts


async def _run_promotions(
    config: WatcherConfig,
    state: dict[str, Any],
    store: StateStore,
    notifier: TelegramNotifier,
    counts: dict[str, int],
) -> None:
    source = config.sources.get("melhores_destinos_rss")
    if not source or not source.enabled:
        LOGGER.info("provider=melhores_destinos_rss disabled")
        return
    previous_run = state["runs"].get("promotions")
    try:
        previous_time = datetime.fromisoformat(previous_run) if previous_run else None
    except ValueError:
        previous_time = None
    posts = await MelhoresDestinosRSS().fetch(config)
    for post in posts:
        item = post.model_dump(mode="json")
        existed = post.promotion_id in state["promotions"]
        state["promotions"][post.promotion_id] = item
        counts["promotions"] += 1
        if existed or previous_time is None:
            continue
        if post.published_at and post.published_at < previous_time - timedelta(minutes=5):
            continue
        if await _deliver(
            store,
            state,
            notifier,
            alert_id=f"promotion:{post.promotion_id}",
            message=format_promotion(post),
        ):
            counts["alerts"] += 1
    LOGGER.info("provider=melhores_destinos_rss matching_posts=%s", len(posts))


async def _run_travelpayouts(
    config: WatcherConfig,
    state: dict[str, Any],
    store: StateStore,
    notifier: TelegramNotifier,
    counts: dict[str, int],
) -> None:
    source = config.sources.get("travelpayouts")
    if not source or not source.enabled:
        LOGGER.info("provider=travelpayouts disabled")
        return
    if not source.terms_accepted or not state["validation"].get("travelpayouts", {}).get("passed"):
        LOGGER.info("provider=travelpayouts skipped until terms are accepted and a live source check passes")
        return
    try:
        provider = TravelpayoutsProvider()
    except ProviderConfigurationError as exc:
        LOGGER.warning("provider=travelpayouts skipped: %s", exc)
        return
    for query in plan_travelpayouts(config, state):
        allowed, reason = reserve_query(state, query, config.schedule.serpapi_max_queries_per_month)
        if not allowed:
            LOGGER.debug("provider=travelpayouts query skipped reason=%s", reason)
            continue
        store.save(state)  # Persist the reservation before making a provider request.
        counts["queries"] += 1
        try:
            observations = await provider.search(query, config)
        except Exception as exc:  # noqa: BLE001 — isolate a failed provider query and preserve other routes.
            mark_query_result(state, query, "error")
            counts["errors"] += 1
            LOGGER.warning("provider=travelpayouts query=%s failed: %s", query.key[:12], exc)
            store.save(state)
            continue
        mark_query_result(state, query, "success" if observations else "empty")
        for fare in observations:
            await _record_fare(fare, config, state, counts)
        store.save(state)


async def _run_serpapi(
    config: WatcherConfig,
    state: dict[str, Any],
    store: StateStore,
    notifier: TelegramNotifier,
    counts: dict[str, int],
) -> None:
    source = config.sources.get("serpapi")
    if not source or not source.enabled:
        LOGGER.info("provider=serpapi disabled")
        return
    if not source.terms_accepted or not state["validation"].get("serpapi", {}).get("passed"):
        LOGGER.info("provider=serpapi skipped until terms are accepted and a live source check passes")
        return
    try:
        provider = SerpApiProvider()
    except ProviderConfigurationError as exc:
        LOGGER.warning("provider=serpapi skipped: %s", exc)
        return
    try:
        free, reason, provider_remaining = await provider.free_quota_available(
            config.schedule.serpapi_max_queries_per_month
        )
    except Exception as exc:  # noqa: BLE001 — fail closed if the free-plan check cannot be verified.
        LOGGER.warning("provider=serpapi skipped: could not verify free quota: %s", exc)
        return
    LOGGER.info("provider=serpapi quota_check=%s", reason)
    if not free:
        return

    queries_reserved = 0
    for query in plan_serpapi(config, state):
        if queries_reserved >= provider_remaining:
            LOGGER.info("provider=serpapi calendar skipped reason=provider_quota_exhausted")
            break
        allowed, reason = reserve_query(state, query, config.schedule.serpapi_max_queries_per_month)
        if not allowed:
            LOGGER.info("provider=serpapi calendar skipped reason=%s", reason)
            continue
        store.save(state)
        queries_reserved += 1
        counts["queries"] += 1
        try:
            prices = await provider.search_calendar(query, config)
        except Exception as exc:  # noqa: BLE001 — isolate a failed provider query and preserve other routes.
            mark_query_result(state, query, "error")
            counts["errors"] += 1
            LOGGER.warning("provider=serpapi calendar query=%s failed: %s", query.key[:12], exc)
            store.save(state)
            continue
        mark_query_result(state, query, "success" if prices else "empty")
        valid_prices = []
        for item in prices:
            try:
                price = Decimal(str(item.get("price")))
                departure = date.fromisoformat(str(item["outbound_date"]))
                returned = date.fromisoformat(str(item["return_date"]))
            except (InvalidOperation, KeyError, TypeError, ValueError):
                continue
            trip_days = (returned - departure).days
            if price > 0 and config.trip.duration_min_days <= trip_days <= config.trip.duration_max_days:
                valid_prices.append((price, departure, returned))
        for price, departure, returned in sorted(valid_prices, key=lambda entry: entry[0]):
            indicative = _calendar_observation(query, price, departure, returned, config)
            if indicative is not None:
                await _record_fare(indicative, config, state, counts)
        store.save(state)

        promising = next(
            (
                (price, departure, returned)
                for price, departure, returned in sorted(valid_prices, key=lambda entry: entry[0])
                if price <= config.alerts.interesting_price_brl_per_person * indicative_passengers(config)
            ),
            None,
        )
        if not promising:
            continue
        price, departure, returned = promising
        detail_queries = [
            plan_details_query(query, departure.isoformat(), returned.isoformat(), direction)
            for direction in ("outbound", "return")
        ]
        # Reserve both directional lookups together; never search outbound without room to complete the return.
        if (
            serapi_queries_used(state) + 2 > config.schedule.serpapi_max_queries_per_month
            or queries_reserved + 2 > provider_remaining
        ):
            LOGGER.info("provider=serpapi details skipped reason=monthly_budget_exhausted")
            continue
        reserved = []
        for detail in detail_queries:
            allowed, _ = reserve_query(state, detail, config.schedule.serpapi_max_queries_per_month)
            if not allowed:
                break
            reserved.append(detail)
        if len(reserved) != 2:
            for detail in reserved:
                mark_query_result(state, detail, "cancelled")
            store.save(state)
            continue
        store.save(state)
        queries_reserved += 2
        counts["queries"] += 2
        try:
            outbound_payload = await provider.search_outbound(
                origin=query.origin,
                destination=query.destination,
                departure=departure.isoformat(),
                returned=returned.isoformat(),
                config=config,
            )
            options = outbound_payload.get("best_flights") or outbound_payload.get("other_flights") or []
            selected = next((item for item in options if isinstance(item, dict) and item.get("departure_token")), None)
            if not selected:
                mark_query_result(state, detail_queries[0], "empty")
                mark_query_result(state, detail_queries[1], "cancelled")
                store.save(state)
                continue
            return_payload = await provider.search_return(str(selected["departure_token"]))
        except Exception as exc:  # noqa: BLE001 — record both reservations when itinerary lookup is incomplete.
            mark_query_result(state, detail_queries[0], "error")
            mark_query_result(state, detail_queries[1], "error")
            counts["errors"] += 1
            LOGGER.warning("provider=serpapi itinerary details failed: %s", exc)
            store.save(state)
            continue
        mark_query_result(state, detail_queries[0], "success")
        mark_query_result(state, detail_queries[1], "success")
        fare = provider.build_observation(
            outbound_payload=outbound_payload,
            return_payload=return_payload,
            origin=query.origin,
            destination=query.destination,
            departure=departure.isoformat(),
            returned=returned.isoformat(),
            calendar_price=price,
            config=config,
        )
        if fare:
            await _record_fare(fare, config, state, counts, notifier, store)
        else:
            LOGGER.info("provider=serpapi details were incomplete or failed itinerary validation")
        store.save(state)


async def _run_source_check(
    config: WatcherConfig,
    state: dict[str, Any],
    store: StateStore,
    counts: dict[str, int],
) -> None:
    """Make bounded real provider calls and unlock only sources with usable data."""
    today = datetime.now(UTC).date()
    origins = [item.code for item in config.origins if item.role in {"primary", "alternative"}]
    destinations = [airport for destination in config.destinations for airport in destination.airports]
    if not origins or not destinations:
        LOGGER.error("source-check needs configured airport origins and destinations")
        return
    origins = origins[:2]
    if len(destinations) > 2:
        destinations = [destinations[0], destinations[len(destinations) // 2]]
    offsets = [config.trip.months_ahead_min, min(config.trip.months_ahead_max, config.trip.months_ahead_min + 3)]
    months: list[tuple[str, str]] = []
    for offset in dict.fromkeys(offsets):
        year, month = divmod((today.year * 12 + today.month - 1 + offset), 12)
        depart_month = f"{year:04d}-{month + 1:02d}"
        return_date = date(year, month + 1, 1) + timedelta(days=config.trip.durations_to_sample_days[0])
        months.append((depart_month, return_date.strftime("%Y-%m")))
    matrix = [(origin, destination, dep, ret) for origin in origins for destination in destinations for dep, ret in months]

    travel_config = config.sources.get("travelpayouts")
    if travel_config and travel_config.enabled and not travel_config.terms_accepted:
        state["validation"]["travelpayouts"] = {
            "passed": False,
            "reason": "set WATCHER_TRAVELPAYOUTS_TERMS_ACCEPTED=true after reviewing the API terms",
        }
        travel_config = None
    if travel_config and travel_config.enabled and not state["validation"].get("travelpayouts", {}).get("passed"):
        try:
            provider = TravelpayoutsProvider()
        except ProviderConfigurationError as exc:
            state["validation"]["travelpayouts"] = {"passed": False, "reason": str(exc)}
        else:
            usable = []
            for origin, destination, dep_month, ret_month in matrix[:8]:
                query = PlannedQuery(
                    provider="travelpayouts",
                    origin=origin,
                    destination=destination,
                    departure_month=dep_month,
                    return_month=ret_month,
                    query_type="source_validation",
                )
                allowed, reason = reserve_query(state, query, config.schedule.serpapi_max_queries_per_month)
                if not allowed:
                    LOGGER.info("source-check travelpayouts route skipped reason=%s", reason)
                    continue
                store.save(state)
                counts["queries"] += 1
                try:
                    fares = await provider.search(query, config)
                    mark_query_result(state, query, "success" if fares else "empty")
                    usable.extend(fares)
                except Exception as exc:  # noqa: BLE001 — isolate a failed provider query and continue validation.
                    mark_query_result(state, query, "error")
                    counts["errors"] += 1
                    LOGGER.warning("source-check travelpayouts route=%s-%s failed: %s", origin, destination, exc)
                store.save(state)
            for fare in usable:
                await _record_fare(fare, config, state, counts)
            state["validation"]["travelpayouts"] = {
                "passed": bool(usable),
                "checked_at": datetime.now(UTC).isoformat(),
                "routes_checked": len(matrix[:8]),
                "usable_prices": len(usable),
                "reason": "cached round-trip prices found" if usable else "no usable cached prices returned",
            }

    serp_config = config.sources.get("serpapi")
    if serp_config and serp_config.enabled and not serp_config.terms_accepted:
        state["validation"]["serpapi"] = {
            "passed": False,
            "reason": "set WATCHER_SERPAPI_TERMS_ACCEPTED=true after reviewing SerpApi terms",
        }
        serp_config = None
    if serp_config and serp_config.enabled and not state["validation"].get("serpapi", {}).get("passed"):
        try:
            provider = SerpApiProvider()
        except ProviderConfigurationError as exc:
            state["validation"]["serpapi"] = {"passed": False, "reason": str(exc)}
        else:
            try:
                free, reason, provider_remaining = await provider.free_quota_available(
                    config.schedule.serpapi_max_queries_per_month
                )
            except Exception as exc:  # noqa: BLE001 — fail closed if the free-plan check cannot be verified.
                free, reason, provider_remaining = False, f"free quota could not be verified: {exc}", 0
            if not free:
                state["validation"]["serpapi"] = {
                    "passed": False,
                    "checked_at": datetime.now(UTC).isoformat(),
                    "reason": reason,
                }
            else:
                candidates: list[tuple[Decimal, PlannedQuery, str, str]] = []
                calendars_checked = 0
                queries_reserved = 0
                for origin, destination, dep_month, ret_month in matrix[:8]:
                    start_year, start_month = (int(part) for part in dep_month.split("-"))
                    end_day = monthrange(start_year, start_month)[1]
                    query = PlannedQuery(
                        provider="serpapi",
                        origin=origin,
                        destination=destination,
                        departure_month=f"{start_year:04d}-{start_month:02d}-01",
                        return_month=f"{start_year:04d}-{start_month:02d}-{end_day:02d}",
                        trip_days=config.trip.durations_to_sample_days[0],
                        query_type="source_validation_calendar",
                    )
                    if (
                        queries_reserved >= provider_remaining
                        or serapi_queries_used(state) >= config.schedule.serpapi_max_queries_per_month
                    ):
                        break
                    allowed, _ = reserve_query(state, query, config.schedule.serpapi_max_queries_per_month)
                    if not allowed:
                        continue
                    store.save(state)
                    queries_reserved += 1
                    counts["queries"] += 1
                    calendars_checked += 1
                    try:
                        prices = await provider.search_calendar(query, config)
                        mark_query_result(state, query, "success" if prices else "empty")
                        for item in prices:
                            try:
                                price = Decimal(str(item["price"]))
                                departure = date.fromisoformat(str(item["outbound_date"]))
                                returned = date.fromisoformat(str(item["return_date"]))
                            except (InvalidOperation, KeyError, TypeError, ValueError):
                                continue
                            duration = (returned - departure).days
                            if price > 0 and config.trip.duration_min_days <= duration <= config.trip.duration_max_days:
                                candidates.append((price, query, departure.isoformat(), returned.isoformat()))
                    except Exception as exc:  # noqa: BLE001 — isolate provider failures by route.
                        mark_query_result(state, query, "error")
                        counts["errors"] += 1
                        LOGGER.warning("source-check serpapi route=%s-%s failed: %s", origin, destination, exc)
                    store.save(state)

                validated_fare = None
                for price, query, departure, returned in sorted(candidates, key=lambda item: item[0]):
                    detail_queries = [
                        PlannedQuery(
                            provider="serpapi",
                            origin=query.origin,
                            destination=query.destination,
                            departure_month=query.departure_month,
                            return_month=query.return_month,
                            trip_days=query.trip_days,
                            query_type=f"source_validation_{direction}_details",
                            outbound_date=departure,
                            return_date=returned,
                        )
                        for direction in ("outbound", "return")
                    ]
                    validation_queries = sum(
                        1
                        for entry in state["query_log"]
                        if entry.get("provider") == "serpapi"
                        and str(entry.get("query_type", "")).startswith("source_validation")
                    )
                    if (
                        validation_queries + 2 > 12
                        or queries_reserved + 2 > provider_remaining
                        or serapi_queries_used(state) + 2 > config.schedule.serpapi_max_queries_per_month
                    ):
                        break
                    reserved = []
                    for detail_query in detail_queries:
                        allowed, _ = reserve_query(
                            state, detail_query, config.schedule.serpapi_max_queries_per_month
                        )
                        if not allowed:
                            break
                        reserved.append(detail_query)
                    if len(reserved) != 2:
                        for detail_query in reserved:
                            mark_query_result(state, detail_query, "cancelled")
                        store.save(state)
                        continue
                    store.save(state)
                    queries_reserved += 2
                    counts["queries"] += 2
                    try:
                        outbound_payload = await provider.search_outbound(
                            origin=query.origin,
                            destination=query.destination,
                            departure=departure,
                            returned=returned,
                            config=config,
                        )
                        options = outbound_payload.get("best_flights") or outbound_payload.get("other_flights") or []
                        selected = next(
                            (item for item in options if isinstance(item, dict) and item.get("departure_token")),
                            None,
                        )
                        if not selected:
                            mark_query_result(state, detail_queries[0], "empty")
                            mark_query_result(state, detail_queries[1], "cancelled")
                            store.save(state)
                            continue
                        return_payload = await provider.search_return(str(selected["departure_token"]))
                        validated_fare = provider.build_observation(
                            outbound_payload=outbound_payload,
                            return_payload=return_payload,
                            origin=query.origin,
                            destination=query.destination,
                            departure=departure,
                            returned=returned,
                            calendar_price=price,
                            config=config,
                        )
                        mark_query_result(state, detail_queries[0], "success" if validated_fare else "invalid_itinerary")
                        mark_query_result(state, detail_queries[1], "success" if validated_fare else "invalid_itinerary")
                    except Exception as exc:  # noqa: BLE001 — fail this candidate while preserving the source check.
                        mark_query_result(state, detail_queries[0], "error")
                        mark_query_result(state, detail_queries[1], "error")
                        counts["errors"] += 1
                        LOGGER.warning("source-check serpapi detail query failed: %s", exc)
                    store.save(state)
                    if validated_fare:
                        await _record_fare(validated_fare, config, state, counts)
                    # A single complete itinerary is enough to validate this source; keep the probe small.
                    break
                state["validation"]["serpapi"] = {
                    "passed": validated_fare is not None,
                    "checked_at": datetime.now(UTC).isoformat(),
                    "calendar_queries": calendars_checked,
                    "complete_round_trips": int(validated_fare is not None),
                    "reason": "complete round-trip details validated" if validated_fare else "no complete valid round trip found",
                }

    store.save(state)
    for name in ("travelpayouts", "serpapi"):
        result = state["validation"].get(name)
        if result:
            LOGGER.info("source-check provider=%s passed=%s reason=%s", name, result.get("passed"), result.get("reason"))


def _calendar_observation(
    query: PlannedQuery,
    price: Decimal,
    departure: date,
    returned: date,
    config: WatcherConfig,
) -> FareObservation | None:
    passengers = indicative_passengers(config)
    try:
        return FareObservation(
            observation_id=f"serp-calendar:{query.origin}:{query.destination}:{departure}:{returned}",
            source="serpapi_google_flights_calendar",
            confidence="cached_indicative",
            observed_at=datetime.now(UTC),
            origin=query.origin,
            destination=query.destination,
            departure_date=departure,
            return_date=returned,
            total_price=price,
            currency=config.app.currency,
            passengers=passengers,
            cabin_class=config.trip.cabin_class,
            notes=["Preço de calendário em cache; conexões e itinerário completo ainda não foram validados."],
        )
    except ValidationError:
        return None


def indicative_passengers(config: WatcherConfig) -> int:
    return config.trip.adults + config.trip.children + config.trip.infants


async def _record_fare(
    fare: FareObservation,
    config: WatcherConfig,
    state: dict[str, Any],
    counts: dict[str, int],
    notifier: TelegramNotifier | None = None,
    store: StateStore | None = None,
) -> None:
    assessment = assess_fare(fare, config, state)
    history = history_summary(fare, state)
    snapshot_key = f"{fare.observation_id}@{fare.observed_at.date().isoformat()}"
    record = fare.model_dump(mode="json")
    state["fares"][snapshot_key] = {**record, "assessment": assessment, "history": history}
    counts["fares"] += 1
    if notifier and store and assessment["eligible_for_immediate_alert"]:
        alert_key = f"fare:{fare.observation_id}:{fare.total_price}"
        if await _deliver(store, state, notifier, alert_key, format_fare_alert(fare, assessment)):
            counts["alerts"] += 1
            state["last_alert_prices"][fare.observation_id] = {
                "price_per_person": assessment["price_per_person"],
                "sent_at": datetime.now(UTC).isoformat(),
            }
            store.save(state)


async def _deliver(
    store: StateStore,
    state: dict[str, Any],
    notifier: TelegramNotifier,
    alert_id: str,
    message: str,
) -> bool:
    if not notifier.enabled:
        LOGGER.info("telegram disabled alert_id=%s", alert_id)
        return False
    if not notifier.configured:
        print(message)
        return False
    all_sent = True
    for chat_id, recipient_key in notifier.recipients:
        delivery_key = f"{alert_id}:{recipient_key}"
        status = state["deliveries"].get(delivery_key, {}).get("status")
        if status in {"sent", "unknown", "pending"}:
            if status != "sent":
                all_sent = False
            continue
        state["deliveries"][delivery_key] = {"status": "pending", "updated_at": datetime.now(UTC).isoformat()}
        store.save(state)
        result: DeliveryResult = await notifier.send_to(chat_id, message)
        state["deliveries"][delivery_key] = {
            "status": result.status,
            "updated_at": datetime.now(UTC).isoformat(),
            "detail": result.detail,
        }
        store.save(state)
        if result.status != "sent":
            all_sent = False
    return all_sent


def _digest_is_due(
    config: WatcherConfig, state: dict[str, Any], now: datetime, forced: bool
) -> bool:
    if forced:
        return True
    local_now = now.astimezone(ZoneInfo("America/Fortaleza"))
    if local_now.hour < config.schedule.daily_digest_hour_fortaleza:
        return False
    last = state["runs"].get("daily_digest")
    if not last:
        return True
    try:
        last_local = datetime.fromisoformat(last).astimezone(ZoneInfo("America/Fortaleza"))
    except ValueError:
        return True
    return last_local.date() < local_now.date()


async def _run_digest(
    config: WatcherConfig,
    state: dict[str, Any],
    store: StateStore,
    notifier: TelegramNotifier,
    counts: dict[str, int],
    now: datetime,
) -> None:
    since = now - timedelta(hours=24)
    fares = []
    for row in state["fares"].values():
        try:
            observed = datetime.fromisoformat(row["observed_at"])
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=UTC)
            if observed >= since and Decimal(str(row["total_price"])) / int(row["passengers"]) <= config.alerts.interesting_price_brl_per_person:
                fare_fields = FareObservation.model_fields
                fares.append(FareObservation.model_validate({key: value for key, value in row.items() if key in fare_fields}))
        except (KeyError, TypeError, ValueError, ValidationError):
            continue
    promotions = []
    for row in state["promotions"].values():
        try:
            published = datetime.fromisoformat(row["published_at"]) if row.get("published_at") else None
            published = published or datetime.fromisoformat(row["discovered_at"])
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
            if published >= since:
                promotions.append(PromotionPost.model_validate(row))
        except (TypeError, ValueError, ValidationError):
            continue
    local_day = now.astimezone(ZoneInfo("America/Fortaleza")).date().isoformat()
    if await _deliver(
        store,
        state,
        notifier,
        f"digest:{local_day}",
        format_daily_digest(fares, promotions),
    ):
        counts["alerts"] += 1

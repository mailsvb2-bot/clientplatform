from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiohttp import web

from clientplatform.application.bot_gateway import (
    admit_telegram_update,
    bot_gateway_health_snapshot,
    claim_due_ingress_events,
    ensure_telegram_customer_link,
    mark_ingress_event_processed,
    reschedule_ingress_event,
    resolve_telegram_route,
)
from clientplatform.domain.bot_gateway import (
    BotGatewayAdmissionRejected,
    BotGatewayError,
    BotGatewayReplayConflict,
    ClaimedIngressEvent,
    ManagedBotRoute,
    ManagedBotRouteNotFound,
)
from clientplatform.runtime.secrets import (
    EnvironmentCredentialProvider,
    SecretReferenceError,
)

log = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < minimum or value > maximum:
        return default
    return value


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < minimum or value > maximum:
        return default
    return value


@dataclass(frozen=True, slots=True)
class BotGatewayRuntimeConfig:
    enabled: bool
    path_prefix: str
    batch_size: int
    interval_seconds: float
    tick_timeout_seconds: float
    lock_ttl_seconds: int
    max_attempts: int
    per_minute_limit: int
    queue_limit: int
    max_payload_bytes: int

    @property
    def telegram_route_path(self) -> str:
        return f"{self.path_prefix}/telegram/{{external_bot_id}}"


def bot_gateway_runtime_config() -> BotGatewayRuntimeConfig:
    prefix = (
        os.getenv("CLIENTPLATFORM_BOT_GATEWAY_PATH_PREFIX")
        or "/clientplatform/managed-bots"
    ).strip()
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    prefix = prefix.rstrip("/")
    if not prefix or "token" in prefix.lower() or "secret" in prefix.lower():
        prefix = "/clientplatform/managed-bots"
    return BotGatewayRuntimeConfig(
        enabled=_env_bool("CLIENTPLATFORM_BOT_GATEWAY_ENABLED", False),
        path_prefix=prefix,
        batch_size=_env_int(
            "CLIENTPLATFORM_BOT_GATEWAY_BATCH_SIZE",
            10,
            minimum=1,
            maximum=100,
        ),
        interval_seconds=_env_float(
            "CLIENTPLATFORM_BOT_GATEWAY_INTERVAL_SEC",
            0.5,
            minimum=0.05,
            maximum=60.0,
        ),
        tick_timeout_seconds=_env_float(
            "CLIENTPLATFORM_BOT_GATEWAY_TICK_TIMEOUT_SEC",
            30.0,
            minimum=1.0,
            maximum=300.0,
        ),
        lock_ttl_seconds=_env_int(
            "CLIENTPLATFORM_BOT_GATEWAY_LOCK_TTL_SEC",
            300,
            minimum=30,
            maximum=3600,
        ),
        max_attempts=_env_int(
            "CLIENTPLATFORM_BOT_GATEWAY_MAX_ATTEMPTS",
            5,
            minimum=1,
            maximum=20,
        ),
        per_minute_limit=_env_int(
            "CLIENTPLATFORM_BOT_GATEWAY_PER_BOT_PER_MINUTE",
            120,
            minimum=1,
            maximum=10_000,
        ),
        queue_limit=_env_int(
            "CLIENTPLATFORM_BOT_GATEWAY_PER_BOT_QUEUE_LIMIT",
            1000,
            minimum=1,
            maximum=100_000,
        ),
        max_payload_bytes=_env_int(
            "CLIENTPLATFORM_BOT_GATEWAY_MAX_PAYLOAD_BYTES",
            262_144,
            minimum=1024,
            maximum=1_048_576,
        ),
    )


class ManagedBotGatewayRuntime:
    """One durable ingress owner for the whole managed Telegram bot fleet."""

    def __init__(
        self,
        *,
        dispatcher: Dispatcher,
        config: BotGatewayRuntimeConfig | None = None,
        credential_provider: EnvironmentCredentialProvider | None = None,
    ) -> None:
        self.config = config or bot_gateway_runtime_config()
        self._dispatcher = dispatcher
        self._credential_provider = credential_provider or EnvironmentCredentialProvider()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._bots: dict[str, tuple[str, Bot]] = {}
        self._iterations = 0
        self._processed = 0
        self._retried = 0
        self._dead = 0
        self._last_error: str | None = None

    def register_route(self, app: web.Application) -> None:
        app["clientplatform_bot_gateway_runtime"] = self
        app.router.add_post(self.config.telegram_route_path, managed_bot_telegram_webhook)

    def start(self) -> bool:
        if not self.config.enabled or self._running:
            return False
        self._running = True
        self._task = asyncio.create_task(
            self._run(),
            name="clientplatform-managed-bot-gateway",
        )
        return True

    async def stop(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        bots = list(self._bots.values())
        self._bots.clear()
        for _, bot in bots:
            await bot.session.close()

    async def handle_webhook(self, request: web.Request) -> web.Response:
        external_bot_id = (request.match_info.get("external_bot_id") or "").strip()
        try:
            route = await asyncio.to_thread(
                resolve_telegram_route,
                external_bot_id=external_bot_id,
            )
            expected_secret = self._credential_provider.resolve(
                route.webhook_secret_reference
            )
        except (ManagedBotRouteNotFound, SecretReferenceError):
            raise web.HTTPNotFound(text="managed bot route not found") from None

        actual_secret = (
            request.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
        ).strip()
        if not actual_secret or not hmac.compare_digest(actual_secret, expected_secret):
            raise web.HTTPForbidden(text="bad managed bot secret")
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="invalid Telegram JSON") from None
        if not isinstance(payload, Mapping):
            raise web.HTTPBadRequest(text="Telegram update must be an object")
        update_id = payload.get("update_id")
        if isinstance(update_id, bool) or not isinstance(update_id, int):
            raise web.HTTPBadRequest(text="Telegram update_id is required")
        try:
            admitted = await asyncio.to_thread(
                admit_telegram_update,
                route=route,
                provider_update_id=update_id,
                payload=payload,
                per_minute_limit=self.config.per_minute_limit,
                queue_limit=self.config.queue_limit,
                max_payload_bytes=self.config.max_payload_bytes,
            )
        except BotGatewayReplayConflict:
            raise web.HTTPConflict(text="conflicting Telegram replay") from None
        except BotGatewayAdmissionRejected as exc:
            raise web.HTTPTooManyRequests(text=str(exc)) from None
        return web.json_response(
            {
                "ok": True,
                "duplicate": admitted.duplicate,
                "event_id": admitted.event.id,
            }
        )

    async def run_tick(self) -> int:
        claimed = await asyncio.to_thread(
            claim_due_ingress_events,
            limit=self.config.batch_size,
            lock_ttl_seconds=self.config.lock_ttl_seconds,
        )
        for item in claimed:
            await self._process_item(item)
        self._iterations += 1
        return len(claimed)

    async def _run(self) -> None:
        while self._running:
            try:
                await asyncio.wait_for(
                    self.run_tick(),
                    timeout=self.config.tick_timeout_seconds,
                )
                self._last_error = None
            except asyncio.TimeoutError:
                self._last_error = "gateway_tick_timeout"
                log.error("Managed bot gateway tick timed out")
            except Exception:  # validator: allow-wide-except
                self._last_error = "gateway_tick_failed"
                log.exception("Managed bot gateway tick failed")
            await asyncio.sleep(self.config.interval_seconds)

    async def _process_item(self, item: ClaimedIngressEvent) -> None:
        try:
            if item.event.payload_json is None:
                raise ValueError("managed bot ingress payload is unavailable")
            payload = json.loads(item.event.payload_json)
            if not isinstance(payload, dict):
                raise ValueError("managed bot ingress payload must be an object")
            actor = _telegram_actor(payload)
            if actor is not None:
                await asyncio.to_thread(
                    ensure_telegram_customer_link,
                    route=item.route,
                    telegram_user_id=actor[0],
                    username=actor[1],
                    display_name=actor[2],
                )
            bot = await self._bot_for(item.route)
            try:
                update = Update.model_validate(payload, context={"bot": bot})
            except AttributeError:
                update = Update(**payload)
            await self._dispatcher.feed_webhook_update(
                bot,
                update,
                managed_bot_business_id=item.route.business_id,
                managed_bot_id=item.route.managed_bot_id,
                managed_bot_connection_id=item.route.connection_id,
            )
            await asyncio.to_thread(mark_ingress_event_processed, item)
            self._processed += 1
        except Exception as exc:  # validator: allow-wide-except
            error_code = _safe_error_code(exc)
            result = await asyncio.to_thread(
                reschedule_ingress_event,
                item,
                error_code=error_code,
                max_attempts=self.config.max_attempts,
            )
            if result.status.value == "dead":
                self._dead += 1
            else:
                self._retried += 1
            log.warning(
                "Managed bot update processing failed",
                extra={
                    "managed_bot_id": item.route.managed_bot_id,
                    "business_id": item.route.business_id,
                    "event_id": item.event.id,
                    "error_code": error_code,
                },
            )

    async def _bot_for(self, route: ManagedBotRoute) -> Bot:
        token = self._credential_provider.resolve(route.credential_reference)
        cached = self._bots.get(route.managed_bot_id)
        if cached is not None and hmac.compare_digest(cached[0], token):
            return cached[1]
        if cached is not None:
            await cached[1].session.close()
        bot = Bot(token=token)
        self._bots[route.managed_bot_id] = (token, bot)
        return bot

    def health_snapshot(self) -> dict[str, Any]:
        database = bot_gateway_health_snapshot() if self.config.enabled else {}
        return {
            "enabled": self.config.enabled,
            "running": self._running,
            "iterations": self._iterations,
            "processed": self._processed,
            "retried": self._retried,
            "dead": self._dead,
            "last_error": self._last_error,
            **database,
        }


async def managed_bot_telegram_webhook(request: web.Request) -> web.Response:
    runtime = request.app.get("clientplatform_bot_gateway_runtime")
    if not isinstance(runtime, ManagedBotGatewayRuntime):
        raise web.HTTPServiceUnavailable(text="managed bot gateway is unavailable")
    return await runtime.handle_webhook(request)


def _telegram_actor(payload: Mapping[str, Any]) -> tuple[int, str | None, str | None] | None:
    actor: Any = None
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        candidate = payload.get(key)
        if isinstance(candidate, Mapping):
            actor = candidate.get("from")
            if isinstance(actor, Mapping):
                break
    if not isinstance(actor, Mapping):
        callback = payload.get("callback_query")
        if isinstance(callback, Mapping):
            actor = callback.get("from")
    if not isinstance(actor, Mapping):
        return None
    raw_id = actor.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
        return None
    username = actor.get("username")
    first_name = str(actor.get("first_name") or "").strip()
    last_name = str(actor.get("last_name") or "").strip()
    display_name = " ".join(part for part in (first_name, last_name) if part) or None
    return raw_id, None if username is None else str(username), display_name


def _safe_error_code(exc: BaseException) -> str:
    if isinstance(exc, SecretReferenceError):
        return "secret_reference_unavailable"
    if isinstance(exc, BotGatewayError):
        return "bot_gateway_domain_error"
    name = type(exc).__name__
    normalized = "".join(
        ("_" + char.lower()) if char.isupper() else char
        for char in name
        if char.isalnum() or char == "_"
    ).strip("_")
    return (normalized or "gateway_processing_failed")[:120]

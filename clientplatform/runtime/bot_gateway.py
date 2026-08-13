from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError, TelegramConflictError
from aiogram.types import Update
from aiohttp import web

from clientplatform.application.bot_gateway import (
    admit_telegram_update,
    bot_gateway_health_snapshot,
    claim_due_ingress_events,
    ensure_telegram_customer_link,
    list_active_telegram_routes,
    mark_ingress_event_processed,
    reschedule_ingress_event,
)
from clientplatform.domain.bot_gateway import (
    BotGatewayAdmissionRejected,
    BotGatewayError,
    BotGatewayLeaseLost,
    ClaimedIngressEvent,
    ManagedBotRoute,
)
from clientplatform.runtime.secrets import (
    EnvironmentCredentialProvider,
    SecretReferenceError,
)
from core.task_manager import TaskManager

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
    return value if minimum <= value <= maximum else default


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


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
    poll_timeout_seconds: int = 20
    reconcile_interval_seconds: float = 2.0

    @property
    def transport(self) -> str:
        return "polling"

    @property
    def telegram_route_path(self) -> str:
        """Compatibility-only path; no Telegram HTTP route is registered."""

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
        poll_timeout_seconds=_env_int(
            "CLIENTPLATFORM_BOT_GATEWAY_POLL_TIMEOUT_SEC",
            20,
            minimum=1,
            maximum=50,
        ),
        reconcile_interval_seconds=_env_float(
            "CLIENTPLATFORM_BOT_GATEWAY_RECONCILE_INTERVAL_SEC",
            2.0,
            minimum=0.1,
            maximum=300.0,
        ),
    )


class ManagedBotGatewayRuntime:
    """Durable long-polling owner for the managed Telegram bot fleet."""

    def __init__(
        self,
        *,
        dispatcher: Dispatcher,
        config: BotGatewayRuntimeConfig | None = None,
        credential_provider: EnvironmentCredentialProvider | None = None,
        task_manager: TaskManager | None = None,
    ) -> None:
        self.config = config or bot_gateway_runtime_config()
        self._dispatcher = dispatcher
        self._credential_provider = (
            credential_provider or EnvironmentCredentialProvider()
        )
        workflow_manager = dispatcher.workflow_data.get("task_manager")
        self._task_manager = (
            task_manager
            if task_manager is not None
            else workflow_manager
            if isinstance(workflow_manager, TaskManager)
            else None
        )
        self._task: asyncio.Task[None] | None = None
        self._pollers: dict[str, asyncio.Task[None]] = {}
        self._routes: dict[str, ManagedBotRoute] = {}
        self._bots: dict[str, tuple[str, Bot]] = {}
        self._running = False
        self._database_snapshot: dict[str, int] = {}
        self._iterations = 0
        self._polled = 0
        self._admitted = 0
        self._duplicates = 0
        self._processed = 0
        self._retried = 0
        self._dead = 0
        self._polling_conflicts = 0
        self._last_error: str | None = None
        self._last_reconcile = 0.0

    def register_route(self, app: web.Application) -> None:
        """Expose health state only; Telegram POST routes are absent."""

        app["clientplatform_bot_gateway_runtime"] = self

    def start(self) -> bool:
        if not self.config.enabled or self._running:
            return False
        if self._task_manager is None:
            raise RuntimeError(
                "Managed Bot Gateway requires the canonical TaskManager"
            )
        self._running = True
        self._task = self._task_manager.create(
            self._run(),
            name="clientplatform-managed-bot-polling-gateway",
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
        for managed_bot_id in list(self._pollers):
            await self._stop_poller(managed_bot_id)
        self._routes.clear()

    async def handle_webhook(self, _request: web.Request) -> web.Response:
        raise web.HTTPNotFound(
            text="Telegram webhook ingress is disabled; use polling"
        )

    async def _stop_poller(self, managed_bot_id: str) -> None:
        task = self._pollers.pop(managed_bot_id, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # validator: allow-wide-except
                log.exception(
                    "Managed Telegram bot poller stopped after failure",
                    extra={"managed_bot_id": managed_bot_id},
                )
        cached = self._bots.pop(managed_bot_id, None)
        if cached is not None:
            await cached[1].session.close()
        self._routes.pop(managed_bot_id, None)

    async def _reconcile_pollers(self) -> None:
        routes = await asyncio.to_thread(list_active_telegram_routes)
        active = {route.managed_bot_id: route for route in routes}

        for managed_bot_id, task in list(self._pollers.items()):
            route = active.get(managed_bot_id)
            known = self._routes.get(managed_bot_id)
            changed = route is not None and known is not None and route != known
            if route is None or task.done() or changed:
                await self._stop_poller(managed_bot_id)

        if not self._running:
            return
        if self._task_manager is None:
            raise RuntimeError("Managed Bot Gateway lost the canonical TaskManager")
        for managed_bot_id, route in active.items():
            if managed_bot_id in self._pollers:
                continue
            self._routes[managed_bot_id] = route
            self._pollers[managed_bot_id] = self._task_manager.create(
                self._poll_route(route),
                name=f"clientplatform-managed-bot-poll-{managed_bot_id}",
            )

    async def _poll_route(self, route: ManagedBotRoute) -> None:
        bot = await self._bot_for(route)
        backoff = 1.0
        offset: int | None = None
        try:
            await self._prepare_polling_bot(bot, route)
            while self._running and self._routes.get(route.managed_bot_id) == route:
                try:
                    updates = await bot.get_updates(
                        offset=offset,
                        limit=100,
                        timeout=self.config.poll_timeout_seconds,
                        allowed_updates=[],
                    )
                    backoff = 1.0
                    for update in updates:
                        payload = _update_payload(update)
                        update_id = int(payload["update_id"])
                        admitted = await asyncio.to_thread(
                            admit_telegram_update,
                            route=route,
                            provider_update_id=update_id,
                            payload=payload,
                            per_minute_limit=self.config.per_minute_limit,
                            queue_limit=self.config.queue_limit,
                            max_payload_bytes=self.config.max_payload_bytes,
                        )
                        offset = update_id + 1
                        self._polled += 1
                        if admitted.duplicate:
                            self._duplicates += 1
                        else:
                            self._admitted += 1
                except BotGatewayAdmissionRejected:
                    self._last_error = "polling_admission_rejected"
                    log.warning(
                        "Managed Telegram polling admission rejected",
                        extra={"managed_bot_id": route.managed_bot_id},
                    )
                    await asyncio.sleep(min(backoff, 10.0))
                    backoff = min(backoff * 2, 30.0)
                except TelegramConflictError:
                    self._polling_conflicts += 1
                    self._last_error = "polling_conflict"
                    log.error(
                        "Managed Telegram polling conflict: another process consumes this bot",
                        extra={"managed_bot_id": route.managed_bot_id},
                    )
                    await asyncio.sleep(min(backoff, 30.0))
                    backoff = min(backoff * 2, 60.0)
                except TelegramAPIError:
                    self._last_error = "polling_provider_error"
                    log.warning(
                        "Managed Telegram polling provider error",
                        extra={"managed_bot_id": route.managed_bot_id},
                        exc_info=True,
                    )
                    await asyncio.sleep(min(backoff, 30.0))
                    backoff = min(backoff * 2, 60.0)
        except asyncio.CancelledError:
            raise
        except Exception:  # validator: allow-wide-except
            self._last_error = "poller_start_failed"
            log.exception(
                "Managed Telegram bot poller failed to start",
                extra={"managed_bot_id": route.managed_bot_id},
            )

    @staticmethod
    async def _prepare_polling_bot(bot: Bot, route: ManagedBotRoute) -> None:
        removed = await bot.delete_webhook(drop_pending_updates=False)
        if removed is not True:
            raise RuntimeError(
                "Telegram did not confirm webhook removal before polling"
            )
        identity = await bot.get_me()
        if str(identity.id) != route.external_bot_id:
            raise RuntimeError(
                "Telegram polling identity does not match managed route"
            )
        expected = str(route.username or "").strip().lower()
        observed = str(identity.username or "").strip().lower()
        if expected and observed != expected:
            raise RuntimeError(
                "Telegram polling username does not match managed route"
            )

    async def run_tick(self) -> int:
        now = time.monotonic()
        if now - self._last_reconcile >= self.config.reconcile_interval_seconds:
            await self._reconcile_pollers()
            self._last_reconcile = now

        claimed = await asyncio.to_thread(
            claim_due_ingress_events,
            limit=self.config.batch_size,
            lock_ttl_seconds=self.config.lock_ttl_seconds,
        )
        for item in claimed:
            try:
                await self._process_item(item)
            except BotGatewayLeaseLost:
                log.warning(
                    "Managed bot ingress lease was lost",
                    extra={"event_id": item.event.id},
                )
            except Exception:  # validator: allow-wide-except
                log.exception(
                    "Managed bot ingress item failed outside retry transition",
                    extra={"event_id": item.event.id},
                )
        self._iterations += 1
        try:
            self._database_snapshot = await asyncio.to_thread(
                bot_gateway_health_snapshot
            )
        except Exception:  # validator: allow-wide-except
            self._last_error = "gateway_health_snapshot_failed"
            log.exception("Managed bot gateway health snapshot failed")
        return len(claimed)

    async def _run(self) -> None:
        while self._running:
            try:
                await asyncio.wait_for(
                    self.run_tick(),
                    timeout=self.config.tick_timeout_seconds,
                )
                if self._last_error == "gateway_health_snapshot_failed":
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
                raise ValueError(
                    "managed bot ingress payload must be an object"
                )
            actor = _telegram_actor(payload)
            if actor is not None:
                customer_link = await asyncio.to_thread(
                    ensure_telegram_customer_link,
                    route=item.route,
                    telegram_user_id=actor[0],
                    username=actor[1],
                    display_name=actor[2],
                )
                from clientplatform.application.sales_intelligence import (
                    extract_customer_message_text,
                    record_managed_bot_customer_message,
                )

                customer_text = extract_customer_message_text(payload)
                if customer_text is not None:
                    ai_enabled = False
                    ai_target = ""
                    try:
                        from clientplatform.runtime.sales_ai_config import SalesAIRuntimeConfig

                        ai_config = SalesAIRuntimeConfig.from_env()
                        ai_enabled = ai_config.enabled
                        ai_target = ai_config.consent_target
                    except (TypeError, ValueError):
                        log.warning(
                            "Sales AI configuration is invalid; continuing without AI",
                            exc_info=True,
                        )
                    try:
                        await asyncio.to_thread(
                            record_managed_bot_customer_message,
                            route=item.route,
                            customer_link=customer_link,
                            telegram_user_id=actor[0],
                            provider_update_id=item.event.provider_update_id,
                            message_text=customer_text,
                            runtime_ai_enabled=ai_enabled,
                            runtime_ai_consent_target=ai_target,
                        )
                    except Exception:  # validator: allow-wide-except
                        log.exception(
                            "Managed bot sales-intelligence side channel failed; dispatch continues",
                            extra={
                                "managed_bot_id": item.route.managed_bot_id,
                                "business_id": item.route.business_id,
                            },
                        )
            bot = await self._bot_for(item.route)
            try:
                update = Update.model_validate(
                    payload,
                    context={"bot": bot},
                )
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
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        cached = self._bots.get(route.managed_bot_id)
        if cached is not None and hmac.compare_digest(cached[0], digest):
            return cached[1]
        if cached is not None:
            await cached[1].session.close()
        bot = Bot(token=token)
        self._bots[route.managed_bot_id] = (digest, bot)
        return bot

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "running": self._running,
            "transport": "polling",
            "active_pollers": sum(
                1 for task in self._pollers.values() if not task.done()
            ),
            "iterations": self._iterations,
            "polled": self._polled,
            "admitted": self._admitted,
            "duplicates": self._duplicates,
            "processed": self._processed,
            "retried": self._retried,
            "dead": self._dead,
            "polling_conflicts": self._polling_conflicts,
            "last_error": self._last_error,
            **self._database_snapshot,
        }


async def managed_bot_telegram_webhook(
    _request: web.Request,
) -> web.Response:
    raise web.HTTPNotFound(
        text="Telegram webhook ingress is disabled; use polling"
    )


def _update_payload(update: Any) -> dict[str, Any]:
    if isinstance(update, Mapping):
        payload = dict(update)
    elif hasattr(update, "model_dump"):
        payload = update.model_dump(mode="json", exclude_none=True)
    else:
        raise TypeError("Telegram polling update cannot be serialized")
    update_id = payload.get("update_id")
    if isinstance(update_id, bool) or not isinstance(update_id, int):
        raise ValueError("Telegram polling update_id is required")
    return payload


def _telegram_actor(
    payload: Mapping[str, Any],
) -> tuple[int, str | None, str | None] | None:
    actor: Any = None
    for key in (
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
    ):
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
    display_name = " ".join(
        part for part in (first_name, last_name) if part
    ) or None
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

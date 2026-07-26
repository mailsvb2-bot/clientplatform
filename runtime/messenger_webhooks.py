from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramAPIError
from aiohttp import web

from config.settings import settings
from runtime.ingress_flags import (
    http_ingress_enabled,
    max_webhook_enabled,
    payment_http_enabled,
    vk_webhook_enabled,
)
from runtime.messenger_ingress_reliability import max_webhook, vk_webhook
from runtime.messenger_media_http import audio_access, audio_media
from runtime.payment_http import payment_terms_web, pay_yookassa_web, yookassa_reconciliation_webhook
from runtime.privacy_export_http import privacy_export_download, privacy_export_landing
from runtime.payment_webhook_admission import ingress_body_limit, payment_webhook_admission_middleware
from runtime.telegram_transport import telegram_transport
from runtime.telegram_webhook_runtime import (
    telegram_legacy_webhook_path,
    telegram_public_webhook_url,
    telegram_webhook,
    telegram_webhook_path,
)
from services.messenger.audio_links import AUDIO_ACCESS_PREFIX, AUDIO_MEDIA_PREFIX
from services.privacy_export_links import PRIVACY_EXPORT_PREFIX, privacy_export_http_enabled
from services.messenger.delivery_pool import start_delivery_worker, stop_delivery_worker

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher

log = logging.getLogger(__name__)


@dataclass
class MessengerWebhookRuntime:
    runner: web.AppRunner
    site: web.TCPSite
    telegram_public_url: str = ""
    delivery_worker_started: bool = False

    async def stop(self) -> None:
        errors: list[BaseException] = []
        if self.delivery_worker_started:
            try:
                await stop_delivery_worker()
            except BaseException as exc:  # validator: allow-wide-except
                log.exception("Messenger delivery worker shutdown failed")
                errors.append(exc)
            finally:
                self.delivery_worker_started = False
        try:
            await self.runner.cleanup()
        except BaseException as exc:  # validator: allow-wide-except
            log.exception("Messenger ingress runner cleanup failed")
            errors.append(exc)
        if errors:
            raise errors[0]


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "http-ingress"})


def _truthy_env(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _deployed_env() -> bool:
    return (os.getenv("APP_ENV") or getattr(settings, "APP_ENV", "") or "dev").strip().lower() in {
        "prod",
        "production",
        "stage",
        "staging",
    }


async def _max_webhook_with_official_secret(request: web.Request) -> web.Response:
    """Map MAX's official secret header onto the stable ingress contract."""

    official = (request.headers.get("X-Max-Bot-Api-Secret") or "").strip()
    legacy_present = any(
        request.headers.get(name)
        for name in (
            "X-Max-Webhook-Secret",
            "X-Webhook-Secret",
            "X-Metrotherapy-Webhook-Secret",
        )
    )
    if official and not legacy_present:
        headers = request.headers.copy()
        headers["X-Max-Webhook-Secret"] = official
        request = request.clone(headers=headers)
    return await max_webhook(request)


def _vk_group_ok(payload: dict[str, Any]) -> bool:
    """Fail closed when a callback belongs to another VK community."""

    expected_raw = str(getattr(settings, "VK_GROUP_ID", "") or "").strip()
    if not expected_raw:
        return not _deployed_env() and _truthy_env("ALLOW_INSECURE_MESSENGER_WEBHOOKS")

    provided_raw = str(payload.get("group_id") or "").strip()
    try:
        expected = int(expected_raw)
        provided = int(provided_raw)
    except (TypeError, ValueError):
        return False
    return expected > 0 and provided == expected


async def _vk_webhook_with_group_guard(request: web.Request) -> web.Response:
    body = await request.text()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return await vk_webhook(request)
    if isinstance(payload, dict) and not _vk_group_ok(payload):
        log.warning("VK webhook rejected: unexpected or missing group_id")
        return web.Response(status=403, text="forbidden")
    return await vk_webhook(request)


def _register_health_routes(app: web.Application) -> None:
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    app.router.add_get("/healthz", _health)


def _register_payment_routes(app: web.Application) -> None:
    app.router.add_get("/terms", payment_terms_web)
    app.router.add_get("/pay/yookassa", pay_yookassa_web)
    app.router.add_post("/pay/yookassa/webhook", yookassa_reconciliation_webhook)


def _register_privacy_export_routes(app: web.Application) -> None:
    app.router.add_get(f"{PRIVACY_EXPORT_PREFIX}{{token}}", privacy_export_landing)
    app.router.add_post(f"{PRIVACY_EXPORT_PREFIX}{{token}}", privacy_export_download)


def _register_max_routes(app: web.Application) -> None:
    app.router.add_post("/webhooks/max", _max_webhook_with_official_secret)


def _register_vk_routes(app: web.Application) -> None:
    app.router.add_post("/webhooks/vk", _vk_webhook_with_group_guard)


def _register_audio_routes(app: web.Application) -> None:
    app.router.add_get(f"{AUDIO_MEDIA_PREFIX}{{filename}}", audio_media)
    app.router.add_get(f"{AUDIO_ACCESS_PREFIX}{{token}}", audio_access)


def _register_telegram_routes(
    app: web.Application,
    *,
    bot: "Bot | None",
    dispatcher: "Dispatcher | None",
) -> str:
    if bot is None or dispatcher is None:
        raise RuntimeError("Telegram webhook transport requires bot and dispatcher")
    app["telegram_bot"] = bot
    app["telegram_dispatcher"] = dispatcher
    app["task_manager"] = dispatcher.workflow_data.get("task_manager")
    app.router.add_post(telegram_webhook_path(), telegram_webhook)
    if _truthy_env("TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED"):
        app.router.add_post(telegram_legacy_webhook_path(), telegram_webhook)
    public_url = telegram_public_webhook_url()
    if not public_url:
        raise RuntimeError("TELEGRAM_WEBHOOK_PUBLIC_BASE_URL is required for telegram webhook transport")
    return public_url


def _resolve_ingress_bind(*, ingress_enabled: bool, telegram_enabled: bool) -> tuple[str, int]:
    ingress_host = getattr(settings, "MESSENGER_WEBHOOK_HOST", "127.0.0.1")
    ingress_port = int(getattr(settings, "MESSENGER_WEBHOOK_PORT", 8081))
    telegram_host = getattr(settings, "TELEGRAM_WEBHOOK_HOST", ingress_host)
    telegram_port = int(getattr(settings, "TELEGRAM_WEBHOOK_PORT", ingress_port))

    if ingress_enabled and telegram_enabled and (
        str(ingress_host) != str(telegram_host) or int(ingress_port) != int(telegram_port)
    ):
        raise RuntimeError("Telegram and HTTP ingress runtimes must share the same ingress host/port")
    if telegram_enabled:
        return str(telegram_host), int(telegram_port)
    return str(ingress_host), int(ingress_port)


async def start_messenger_webhook_runtime(
    bot: "Bot | None" = None,
    dispatcher: "Dispatcher | None" = None,
) -> MessengerWebhookRuntime | None:
    payment_enabled = payment_http_enabled()
    privacy_export_enabled = privacy_export_http_enabled()
    max_enabled = max_webhook_enabled()
    vk_enabled = vk_webhook_enabled()
    ingress_enabled = http_ingress_enabled()
    telegram_enabled = telegram_transport() == "webhook"
    if not ingress_enabled and not telegram_enabled:
        return None

    app = web.Application(
        client_max_size=ingress_body_limit(),
        middlewares=[payment_webhook_admission_middleware],
    )
    _register_health_routes(app)

    if payment_enabled:
        _register_payment_routes(app)
    if privacy_export_enabled:
        _register_privacy_export_routes(app)
    if max_enabled:
        _register_max_routes(app)
    if vk_enabled:
        _register_vk_routes(app)
    if max_enabled or vk_enabled:
        _register_audio_routes(app)

    telegram_public_url = ""
    if telegram_enabled:
        telegram_public_url = _register_telegram_routes(app, bot=bot, dispatcher=dispatcher)

    host, port = _resolve_ingress_bind(
        ingress_enabled=ingress_enabled,
        telegram_enabled=telegram_enabled,
    )

    runner = web.AppRunner(app)
    await runner.setup()
    delivery_worker_started = False
    try:
        site = web.TCPSite(runner, host=host, port=port)
        await site.start()

        if max_enabled or vk_enabled:
            start_delivery_worker()
            delivery_worker_started = True

        if telegram_enabled:
            if bot is None:
                raise RuntimeError("Telegram webhook bot disappeared after route registration")
            await bot.set_webhook(
                url=telegram_public_url,
                secret_token=(getattr(settings, "TELEGRAM_WEBHOOK_SECRET_TOKEN", "") or "") or None,
                drop_pending_updates=bool(
                    getattr(settings, "TELEGRAM_WEBHOOK_DROP_PENDING_UPDATES", False) or False
                ),
            )
            log.info(
                "Telegram webhook runtime started on %s:%s, public_url=%s",
                host,
                port,
                telegram_public_url,
            )

        log.info(
            "HTTP ingress started on %s:%s payment=%s privacy_export=%s max=%s vk=%s durable_delivery=%s",
            host,
            port,
            payment_enabled,
            privacy_export_enabled,
            max_enabled,
            vk_enabled,
            delivery_worker_started,
        )
        return MessengerWebhookRuntime(
            runner=runner,
            site=site,
            telegram_public_url=telegram_public_url,
            delivery_worker_started=delivery_worker_started,
        )
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError, TelegramAPIError):
        if delivery_worker_started:
            try:
                await stop_delivery_worker()
            except BaseException:  # validator: allow-wide-except
                log.exception("Messenger delivery worker startup rollback failed")
        try:
            await runner.cleanup()
        except BaseException:  # validator: allow-wide-except
            log.exception("Messenger ingress startup rollback cleanup failed")
        raise

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp import web

from clientplatform.application.ad_connections import (
    ad_connections_enabled,
    yandex_direct_provider_configured,
)
from clientplatform.runtime.ad_publication_worker import AdPublicationWorker
from clientplatform.runtime.bot_gateway import bot_gateway_runtime_config
from clientplatform.runtime.messenger_channel_ingress import (
    canonical_max_webhook,
    canonical_vk_webhook,
)
from clientplatform.runtime.partner_aware_bot_gateway import ManagedBotGatewayRuntime
from config.settings import settings
from core.task_manager import TaskManager
from runtime.ad_oauth_http import ad_oauth_http_enabled, register_ad_oauth_routes
from runtime.ingress_flags import (
    http_ingress_enabled,
    max_webhook_enabled,
    payment_http_enabled,
    vk_webhook_enabled,
)
from runtime.messenger_ingress_reliability import max_webhook, vk_webhook
from runtime.messenger_media_http import audio_access, audio_media
from runtime.payment_http import (
    payment_terms_web,
    pay_yookassa_web,
    yookassa_reconciliation_webhook,
)
from runtime.payment_webhook_admission import (
    ingress_body_limit,
    payment_webhook_admission_middleware,
)
from runtime.privacy_export_http import privacy_export_download, privacy_export_landing
from services.messenger.audio_links import AUDIO_ACCESS_PREFIX, AUDIO_MEDIA_PREFIX
from services.messenger.delivery_pool import start_delivery_worker, stop_delivery_worker
from services.privacy_export_links import PRIVACY_EXPORT_PREFIX, privacy_export_http_enabled

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher

log = logging.getLogger(__name__)


@dataclass
class MessengerWebhookRuntime:
    runner: web.AppRunner
    site: web.TCPSite
    # Retained as a compatibility field for callers and health serializers. It is
    # always empty because Telegram never registers on the HTTP ingress runtime.
    telegram_public_url: str = ""
    delivery_worker_started: bool = False
    bot_gateway_runtime: ManagedBotGatewayRuntime | None = None
    ad_publication_worker: AdPublicationWorker | None = None

    async def stop(self) -> None:
        errors: list[BaseException] = []
        if self.ad_publication_worker is not None:
            try:
                await self.ad_publication_worker.stop()
            except BaseException as exc:  # validator: allow-wide-except
                log.exception("Advertising publication worker shutdown failed")
                errors.append(exc)
            finally:
                self.ad_publication_worker = None
        if self.bot_gateway_runtime is not None:
            try:
                await self.bot_gateway_runtime.stop()
            except BaseException as exc:  # validator: allow-wide-except
                log.exception("Managed bot gateway shutdown failed")
                errors.append(exc)
            finally:
                self.bot_gateway_runtime = None
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
    gateway = request.app.get("clientplatform_bot_gateway_runtime")
    payload: dict[str, Any] = {"ok": True, "service": "http-ingress"}
    if isinstance(gateway, ManagedBotGatewayRuntime):
        payload["managed_bot_gateway"] = gateway.health_snapshot()
    if request.app.get("clientplatform_ad_oauth_bot") is not None:
        payload["ad_oauth"] = True
    if request.app.get("clientplatform_omnichannel_ingress") is True:
        payload["omnichannel_ingress"] = True
    return web.json_response(payload)


def _truthy_env(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _deployed_env() -> bool:
    return (
        os.getenv("APP_ENV") or getattr(settings, "APP_ENV", "") or "dev"
    ).strip().lower() in {
        "prod",
        "production",
        "stage",
        "staging",
    }


def _omnichannel_ingress_enabled() -> bool:
    return _truthy_env("CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED")


async def _max_webhook_with_official_secret(request: web.Request) -> web.Response:
    """Map MAX's official secret header onto the stable legacy ingress contract."""

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
    """Fail closed when a legacy callback belongs to another VK community."""

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


def _register_clientplatform_omnichannel_routes(app: web.Application) -> None:
    app.router.add_post(
        "/clientplatform/webhooks/vk/{route_id}",
        canonical_vk_webhook,
    )
    app.router.add_post(
        "/clientplatform/webhooks/max/{route_id}",
        canonical_max_webhook,
    )
    app["clientplatform_omnichannel_ingress"] = True


def _register_audio_routes(app: web.Application) -> None:
    app.router.add_get(f"{AUDIO_MEDIA_PREFIX}{{filename}}", audio_media)
    app.router.add_get(f"{AUDIO_ACCESS_PREFIX}{{token}}", audio_access)


def _resolve_ingress_bind() -> tuple[str, int]:
    return (
        str(getattr(settings, "MESSENGER_WEBHOOK_HOST", "127.0.0.1")),
        int(getattr(settings, "MESSENGER_WEBHOOK_PORT", 8081)),
    )


def _ad_publication_worker_enabled() -> bool:
    """Run durable ad work whenever the provider connection is configured."""

    return ad_connections_enabled() and yandex_direct_provider_configured()


async def start_messenger_webhook_runtime(
    bot: "Bot | None" = None,
    dispatcher: "Dispatcher | None" = None,
) -> MessengerWebhookRuntime | None:
    """Start webhook providers, OAuth callbacks and durable provider workers."""

    payment_enabled = payment_http_enabled()
    privacy_export_enabled = privacy_export_http_enabled()
    max_enabled = max_webhook_enabled()
    vk_enabled = vk_webhook_enabled()
    omnichannel_enabled = _omnichannel_ingress_enabled()
    ad_oauth_enabled = ad_oauth_http_enabled()
    ad_worker_enabled = _ad_publication_worker_enabled()
    gateway_config = bot_gateway_runtime_config()
    gateway_enabled = gateway_config.enabled
    ingress_enabled = (
        http_ingress_enabled()
        or gateway_enabled
        or ad_oauth_enabled
        or ad_worker_enabled
        or omnichannel_enabled
    )
    if not ingress_enabled:
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
    if omnichannel_enabled:
        _register_clientplatform_omnichannel_routes(app)
    if max_enabled or vk_enabled:
        _register_audio_routes(app)
    if ad_oauth_enabled:
        if bot is None:
            raise RuntimeError("Advertising OAuth callback requires the central bot")
        register_ad_oauth_routes(app, bot=bot)

    bot_gateway_runtime: ManagedBotGatewayRuntime | None = None
    if gateway_enabled:
        if dispatcher is None:
            raise RuntimeError("Managed bot polling gateway requires dispatcher")
        bot_gateway_runtime = ManagedBotGatewayRuntime(
            dispatcher=dispatcher,
            config=gateway_config,
        )
        bot_gateway_runtime.register_route(app)

    ad_publication_worker: AdPublicationWorker | None = None
    if ad_worker_enabled:
        if dispatcher is None:
            raise RuntimeError("Advertising publication worker requires dispatcher")
        workflow_manager = dispatcher.workflow_data.get("task_manager")
        if not isinstance(workflow_manager, TaskManager):
            raise RuntimeError("Advertising publication worker requires canonical TaskManager")
        ad_publication_worker = AdPublicationWorker.from_environment(
            task_manager=workflow_manager
        )

    host, port = _resolve_ingress_bind()
    runner = web.AppRunner(app)
    await runner.setup()
    delivery_worker_started = False
    gateway_started = False
    ad_worker_started = False
    try:
        site = web.TCPSite(runner, host=host, port=port)
        await site.start()

        if max_enabled or vk_enabled:
            start_delivery_worker()
            delivery_worker_started = True
        if bot_gateway_runtime is not None:
            gateway_started = bot_gateway_runtime.start()
            if not gateway_started:
                raise RuntimeError("Managed bot polling gateway failed to start")
        if ad_publication_worker is not None:
            ad_worker_started = ad_publication_worker.start()
            if not ad_worker_started:
                raise RuntimeError("Advertising publication worker failed to start")

        log.info(
            "HTTP ingress started on %s:%s payment=%s privacy_export=%s "
            "max=%s vk=%s omnichannel=%s durable_delivery=%s managed_bot_polling=%s "
            "ad_oauth=%s ad_publication_worker=%s",
            host,
            port,
            payment_enabled,
            privacy_export_enabled,
            max_enabled,
            vk_enabled,
            omnichannel_enabled,
            delivery_worker_started,
            gateway_started,
            ad_oauth_enabled,
            ad_worker_enabled,
        )
        return MessengerWebhookRuntime(
            runner=runner,
            site=site,
            telegram_public_url="",
            delivery_worker_started=delivery_worker_started,
            bot_gateway_runtime=bot_gateway_runtime,
            ad_publication_worker=ad_publication_worker,
        )
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        if ad_worker_started and ad_publication_worker is not None:
            try:
                await ad_publication_worker.stop()
            except BaseException:  # validator: allow-wide-except
                log.exception("Advertising publication worker startup rollback failed")
        if gateway_started and bot_gateway_runtime is not None:
            try:
                await bot_gateway_runtime.stop()
            except BaseException:  # validator: allow-wide-except
                log.exception("Managed bot gateway startup rollback failed")
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

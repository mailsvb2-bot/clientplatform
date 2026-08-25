from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

from clientplatform.application.dispatch_worker import DispatchBatchResult, run_dispatch_batch
from clientplatform.application.program_media import run_program_media_cleanup_batch
from clientplatform.application.sales_followups import run_sales_followup_maintenance_batch
from clientplatform.runtime.control_bot import control_bot_enabled
from clientplatform.runtime.max_two_phase_media import TwoPhaseMaxRuntimeClient
from clientplatform.runtime.messenger_provider_clients import VkRuntimeClient
from clientplatform.runtime.native_messenger_setup_links import NativeMessengerSetupLinkService
from clientplatform.runtime.secrets import EnvironmentCredentialProvider
from clientplatform.transport import (
    AdapterRegistry,
    MaxDispatchAdapter,
    TelegramDispatchAdapter,
    VkDispatchAdapter,
)
from clientplatform.transport.media import (
    HmacMediaGatewayResolver,
    MediaReferenceResolver,
    SafeMediaReferenceResolver,
)
from clientplatform.transport.telegram_http import AiohttpTelegramBotClient
from core.runtime_env import env_float, env_int


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
log = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() in _TRUE_VALUES


def _canonical_omnichannel_enabled() -> bool:
    return _env_bool("CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED", False)


@dataclass(frozen=True, slots=True)
class DispatchRuntimeConfig:
    enabled: bool
    interval_seconds: float
    tick_timeout_seconds: float
    batch_size: int
    max_attempts: int
    lock_ttl_seconds: int
    http_timeout_seconds: float
    media_gateway_base_url: str = ""
    media_signing_secret_reference: str = "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"
    media_url_ttl_seconds: int = 300
    media_multipart_max_bytes: int = 20_000_000
    media_cleanup_batch_size: int = 10
    media_cleanup_max_attempts: int = 12
    media_cleanup_lock_ttl_seconds: int = 900


@dataclass(frozen=True, slots=True)
class DispatchRuntime:
    config: DispatchRuntimeConfig
    credential_provider: EnvironmentCredentialProvider
    adapters: AdapterRegistry
    interaction_link_resolver: Callable[..., str | None] | None = None


def dispatch_runtime_config() -> DispatchRuntimeConfig:
    default_enabled = bool(control_bot_enabled() or _canonical_omnichannel_enabled())
    return DispatchRuntimeConfig(
        enabled=_env_bool("CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED", default_enabled),
        interval_seconds=env_float(
            "CLIENTPLATFORM_DISPATCH_INTERVAL_SEC",
            5.0,
            minimum=1.0,
            maximum=300.0,
        ),
        tick_timeout_seconds=env_float(
            "CLIENTPLATFORM_DISPATCH_TICK_TIMEOUT_SEC",
            120.0,
            minimum=5.0,
            maximum=1800.0,
        ),
        batch_size=env_int(
            "CLIENTPLATFORM_DISPATCH_BATCH_SIZE",
            10,
            minimum=1,
            maximum=100,
        ),
        max_attempts=env_int(
            "CLIENTPLATFORM_DISPATCH_MAX_ATTEMPTS",
            8,
            minimum=1,
            maximum=100,
        ),
        lock_ttl_seconds=env_int(
            "CLIENTPLATFORM_DISPATCH_LOCK_TTL_SEC",
            900,
            minimum=30,
            maximum=86_400,
        ),
        http_timeout_seconds=env_float(
            "CLIENTPLATFORM_TELEGRAM_HTTP_TIMEOUT_SEC",
            20.0,
            minimum=1.0,
            maximum=120.0,
        ),
        media_gateway_base_url=str(
            os.getenv("CLIENTPLATFORM_MEDIA_GATEWAY_BASE_URL") or ""
        ).strip(),
        media_signing_secret_reference=str(
            os.getenv("CLIENTPLATFORM_MEDIA_SIGNING_SECRET_REFERENCE")
            or "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"
        ).strip(),
        media_url_ttl_seconds=env_int(
            "CLIENTPLATFORM_MEDIA_URL_TTL_SEC",
            300,
            minimum=60,
            maximum=900,
        ),
        media_multipart_max_bytes=env_int(
            "CLIENTPLATFORM_PROGRAM_MEDIA_MAX_BYTES",
            20_000_000,
            minimum=1,
            maximum=20_000_000,
        ),
        media_cleanup_batch_size=env_int(
            "CLIENTPLATFORM_PROGRAM_MEDIA_CLEANUP_BATCH_SIZE",
            10,
            minimum=1,
            maximum=100,
        ),
        media_cleanup_max_attempts=env_int(
            "CLIENTPLATFORM_PROGRAM_MEDIA_CLEANUP_MAX_ATTEMPTS",
            12,
            minimum=1,
            maximum=100,
        ),
        media_cleanup_lock_ttl_seconds=env_int(
            "CLIENTPLATFORM_PROGRAM_MEDIA_CLEANUP_LOCK_TTL_SEC",
            900,
            minimum=30,
            maximum=86_400,
        ),
    )


def _build_media_resolver(
    config: DispatchRuntimeConfig,
    credential_provider: EnvironmentCredentialProvider,
) -> MediaReferenceResolver:
    if not config.media_gateway_base_url:
        return SafeMediaReferenceResolver()
    return HmacMediaGatewayResolver(
        base_url=config.media_gateway_base_url,
        credential_provider=credential_provider,
        signing_secret_reference=config.media_signing_secret_reference,
        ttl_seconds=config.media_url_ttl_seconds,
    )


def build_dispatch_runtime(
    config: DispatchRuntimeConfig | None = None,
) -> DispatchRuntime:
    selected = config or dispatch_runtime_config()
    credential_provider = EnvironmentCredentialProvider()
    telegram_client = AiohttpTelegramBotClient(
        timeout_seconds=selected.http_timeout_seconds,
        multipart_media_base_url=selected.media_gateway_base_url,
        multipart_max_bytes=selected.media_multipart_max_bytes,
    )
    media_resolver = _build_media_resolver(selected, credential_provider)
    setup_link_service = NativeMessengerSetupLinkService(
        credential_provider=credential_provider,
    )
    adapters = AdapterRegistry(
        [
            TelegramDispatchAdapter(
                telegram_client,
                media_resolver=media_resolver,
            ),
            VkDispatchAdapter(
                VkRuntimeClient(),
                media_resolver=media_resolver,
            ),
            MaxDispatchAdapter(
                TwoPhaseMaxRuntimeClient(),
                media_resolver=media_resolver,
            ),
        ]
    )
    return DispatchRuntime(
        config=selected,
        credential_provider=credential_provider,
        adapters=adapters,
        interaction_link_resolver=setup_link_service.resolve_command_url,
    )


async def run_configured_dispatch_tick(
    runtime: DispatchRuntime | None = None,
) -> DispatchBatchResult:
    selected = runtime or build_dispatch_runtime()
    if not selected.config.enabled:
        return DispatchBatchResult(claimed=0, sent=0, retried=0, dead=0)
    try:
        await asyncio.to_thread(
            run_sales_followup_maintenance_batch,
            limit=max(10, selected.config.batch_size * 5),
        )
    except Exception:  # validator: allow-wide-except - reminder maintenance must not block delivery
        log.exception("Sales follow-up maintenance tick failed")
    try:
        await asyncio.to_thread(
            run_program_media_cleanup_batch,
            limit=selected.config.media_cleanup_batch_size,
            max_attempts=selected.config.media_cleanup_max_attempts,
            lock_ttl_seconds=selected.config.media_cleanup_lock_ttl_seconds,
        )
    except Exception:  # validator: allow-wide-except - cleanup must not block customer delivery
        log.exception("Program media cleanup tick failed")
    return await run_dispatch_batch(
        credential_provider=selected.credential_provider,
        adapters=selected.adapters,
        limit=selected.config.batch_size,
        max_attempts=selected.config.max_attempts,
        lock_ttl_seconds=selected.config.lock_ttl_seconds,
        interaction_link_resolver=selected.interaction_link_resolver,
    )

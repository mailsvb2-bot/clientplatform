from __future__ import annotations

import os
from dataclasses import dataclass

from a1.application.dispatch_worker import DispatchBatchResult, run_dispatch_batch
from a1.runtime.secrets import EnvironmentCredentialProvider
from a1.transport import AdapterRegistry, TelegramDispatchAdapter
from a1.transport.telegram_http import AiohttpTelegramBotClient
from core.runtime_env import env_float, env_int


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() in _TRUE_VALUES


@dataclass(frozen=True, slots=True)
class DispatchRuntimeConfig:
    enabled: bool
    interval_seconds: float
    batch_size: int
    max_attempts: int
    lock_ttl_seconds: int
    http_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class DispatchRuntime:
    config: DispatchRuntimeConfig
    credential_provider: EnvironmentCredentialProvider
    adapters: AdapterRegistry


def dispatch_runtime_config() -> DispatchRuntimeConfig:
    return DispatchRuntimeConfig(
        enabled=_env_bool("A1_DISPATCH_RUNTIME_ENABLED", False),
        interval_seconds=env_float(
            "A1_DISPATCH_INTERVAL_SEC",
            5.0,
            minimum=1.0,
            maximum=300.0,
        ),
        batch_size=env_int(
            "A1_DISPATCH_BATCH_SIZE",
            10,
            minimum=1,
            maximum=100,
        ),
        max_attempts=env_int(
            "A1_DISPATCH_MAX_ATTEMPTS",
            8,
            minimum=1,
            maximum=100,
        ),
        lock_ttl_seconds=env_int(
            "A1_DISPATCH_LOCK_TTL_SEC",
            900,
            minimum=30,
            maximum=86_400,
        ),
        http_timeout_seconds=env_float(
            "A1_TELEGRAM_HTTP_TIMEOUT_SEC",
            20.0,
            minimum=1.0,
            maximum=120.0,
        ),
    )


def build_dispatch_runtime(
    config: DispatchRuntimeConfig | None = None,
) -> DispatchRuntime:
    selected = config or dispatch_runtime_config()
    credential_provider = EnvironmentCredentialProvider()
    telegram_client = AiohttpTelegramBotClient(
        timeout_seconds=selected.http_timeout_seconds,
    )
    adapters = AdapterRegistry([TelegramDispatchAdapter(telegram_client)])
    return DispatchRuntime(
        config=selected,
        credential_provider=credential_provider,
        adapters=adapters,
    )


async def run_configured_dispatch_tick(
    runtime: DispatchRuntime | None = None,
) -> DispatchBatchResult:
    selected = runtime or build_dispatch_runtime()
    if not selected.config.enabled:
        return DispatchBatchResult(claimed=0, sent=0, retried=0, dead=0)
    return await run_dispatch_batch(
        credential_provider=selected.credential_provider,
        adapters=selected.adapters,
        limit=selected.config.batch_size,
        max_attempts=selected.config.max_attempts,
        lock_ttl_seconds=selected.config.lock_ttl_seconds,
    )

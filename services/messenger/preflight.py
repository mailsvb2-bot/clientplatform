from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import settings
from runtime.ingress_flags import max_webhook_enabled, vk_webhook_enabled
from runtime.telegram_transport import telegram_transport, telegram_webhook_requested
from services.messenger.delivery_health import delivery_health_snapshot


@dataclass(frozen=True)
class MessengerPreflightStatus:
    channel: str
    ok: bool
    missing: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    details: dict[str, Any] | None = None


def _value(name: str, default: Any = "") -> Any:
    return getattr(settings, name, default)


def _env_or_setting(name: str, default: Any = "") -> Any:
    raw = os.getenv(name)
    return raw if raw is not None else _value(name, default)


def _app_env() -> str:
    return (os.getenv("APP_ENV") or getattr(settings, "APP_ENV", "") or "dev").strip().lower()


def _deployed_env() -> bool:
    return _app_env() in {"prod", "production", "stage", "staging"}


def _public_webhook_url(path: str) -> str:
    base = str(_value("MESSENGER_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    return f"{base}{path}" if base else ""


def _missing(*names: str) -> tuple[str, ...]:
    out: list[str] = []
    for name in names:
        value = _env_or_setting(name, "")
        if isinstance(value, bool):
            if not value:
                out.append(name)
        elif not str(value or "").strip():
            out.append(name)
    return tuple(out)


def _https_warning(name: str, value: str, warnings: list[str]) -> None:
    clean = (value or "").strip()
    if clean and not clean.startswith("https://"):
        warnings.append(f"{name} should start with https:// in deployed environments")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "webhook"}


def _explicitly_disabled(name: str) -> bool:
    raw = os.getenv(name)
    return raw is not None and str(raw).strip().lower() in {"0", "false", "no", "off"}


def _positive_int_env(name: str, default: int, *, minimum: int = 1, maximum: int = 1_000_000) -> int:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return min(max(value, minimum), maximum)


def check_telegram_preflight() -> MessengerPreflightStatus:
    missing: list[str] = []
    warnings: list[str] = []
    if not str(_value("BOT_TOKEN", "") or "").strip():
        missing.append("BOT_TOKEN")

    transport = telegram_transport()
    stale_webhook_requested = telegram_webhook_requested()
    if stale_webhook_requested:
        warnings.append(
            "Stale Telegram webhook configuration is ignored; runtime remains polling-only"
        )

    return MessengerPreflightStatus(
        channel="telegram",
        ok=not missing,
        missing=tuple(missing),
        warnings=tuple(warnings),
        details={
            "enabled": _deployed_env(),
            "transport": transport,
            "webhook_enabled": False,
            "webhook_requested": stale_webhook_requested,
        },
    )



def check_vk_preflight() -> MessengerPreflightStatus:
    enabled = vk_webhook_enabled()
    if not enabled:
        return MessengerPreflightStatus(channel="vk", ok=True, details={"enabled": False})

    required = ["VK_GROUP_TOKEN", "VK_CONFIRMATION_TOKEN", "VK_GROUP_ID", "MESSENGER_PUBLIC_BASE_URL"]
    if _deployed_env():
        required.append("VK_SECRET")
    missing = list(_missing(*required))
    warnings: list[str] = []

    group_id_raw = str(_env_or_setting("VK_GROUP_ID", "") or "").strip()
    if group_id_raw:
        try:
            group_id = int(group_id_raw)
        except ValueError:
            group_id = 0
        if group_id <= 0:
            missing.append("VK_GROUP_ID(valid positive integer)")

    if not str(_value("VK_SECRET", "") or "").strip():
        warnings.append("VK_SECRET is not configured; VK webhook secret verification is not enforced")
    if _deployed_env():
        _https_warning("MESSENGER_PUBLIC_BASE_URL", str(_value("MESSENGER_PUBLIC_BASE_URL", "") or ""), warnings)

    api_version = str(_env_or_setting("VK_API_VERSION", "5.199") or "5.199").strip()
    if api_version != "5.199":
        warnings.append("VK_API_VERSION differs from the audited official 5.199 schema")

    callback_ack_enabled = _truthy(_env_or_setting("VK_CALLBACK_SNACKBAR_ENABLED", "1"))
    if not callback_ack_enabled:
        warnings.append("VK_CALLBACK_SNACKBAR_ENABLED is off; callback buttons may keep a pending UI state")

    return MessengerPreflightStatus(
        channel="vk",
        ok=not missing,
        missing=tuple(sorted(set(missing))),
        warnings=tuple(warnings),
        details={
            "enabled": True,
            "webhook_url": _public_webhook_url("/webhooks/vk"),
            "api_version": api_version,
            "callback_ack_enabled": callback_ack_enabled,
        },
    )


def check_max_preflight() -> MessengerPreflightStatus:
    enabled = max_webhook_enabled()
    if not enabled:
        return MessengerPreflightStatus(channel="max", ok=True, details={"enabled": False})

    required = ["MAX_BOT_TOKEN", "MAX_BOT_LINK_BASE", "MESSENGER_PUBLIC_BASE_URL"]
    if _deployed_env():
        required.append("MAX_WEBHOOK_SECRET")
    missing = list(_missing(*required))
    warnings: list[str] = []
    if not str(_value("MAX_WEBHOOK_SECRET", "") or "").strip():
        warnings.append("MAX_WEBHOOK_SECRET is not configured; MAX webhook secret verification is not enforced")
    if _deployed_env():
        _https_warning("MESSENGER_PUBLIC_BASE_URL", str(_value("MESSENGER_PUBLIC_BASE_URL", "") or ""), warnings)

    api_base = str(
        os.getenv("MAX_API_BASE_URL")
        or _value("MAX_API_BASE_URL", "")
        or "https://platform-api2.max.ru"
    ).strip().rstrip("/")
    if api_base in {"https://platform-api.max.ru", "https://botapi.max.ru"}:
        if _deployed_env():
            missing.append("MAX_API_BASE_URL(official_api2_required)")
        else:
            warnings.append("MAX_API_BASE_URL uses a legacy domain; migrate to https://platform-api2.max.ru")
    elif api_base != "https://platform-api2.max.ru":
        if _deployed_env():
            missing.append("MAX_API_BASE_URL(official_api2_required)")
        else:
            warnings.append("MAX_API_BASE_URL should use the official https://platform-api2.max.ru domain")

    ca_bundle = str(os.getenv("MAX_CA_BUNDLE") or _value("MAX_CA_BUNDLE", "") or "").strip()
    if ca_bundle and not Path(ca_bundle).is_file():
        if _deployed_env():
            missing.append("MAX_CA_BUNDLE(existing_file)")
        else:
            warnings.append("MAX_CA_BUNDLE points to a missing file")

    return MessengerPreflightStatus(
        channel="max",
        ok=not missing,
        missing=tuple(sorted(set(missing))),
        warnings=tuple(warnings),
        details={
            "enabled": True,
            "webhook_url": _public_webhook_url("/webhooks/max"),
            "api_base": api_base,
            "ca_bundle_configured": bool(ca_bundle),
        },
    )


def check_delivery_outbox_preflight() -> MessengerPreflightStatus:
    enabled = bool(vk_webhook_enabled() or max_webhook_enabled())
    if not enabled:
        return MessengerPreflightStatus(channel="delivery_outbox", ok=True, details={"enabled": False})

    try:
        snapshot = delivery_health_snapshot()
    except (sqlite3.Error, RuntimeError) as exc:
        return MessengerPreflightStatus(
            channel="delivery_outbox",
            ok=False,
            missing=("messenger_delivery_outbox(unavailable)",),
            warnings=(f"delivery health unavailable: {type(exc).__name__}",),
            details={"enabled": True, "worker_running": False},
        )

    missing: list[str] = []
    warnings: list[str] = []
    worker_expected = bool(snapshot.get("worker_expected"))
    if not bool(snapshot.get("worker_running")):
        missing.append("delivery_worker(running)")
    dead = int(snapshot.get("dead") or 0)
    pending = int(snapshot.get("pending") or 0)
    retry = int(snapshot.get("retry") or 0)
    if dead > 0:
        missing.append(f"dead_letters={dead}")
    if retry > 0:
        warnings.append(f"delivery retries pending: {retry}")

    pending_age = int(snapshot.get("oldest_pending_age_sec") or 0)
    retry_age = int(snapshot.get("oldest_retry_age_sec") or 0)
    sending_age = int(snapshot.get("oldest_sending_age_sec") or 0)
    pending_limit = _positive_int_env("MESSENGER_OUTBOX_READY_MAX_PENDING_AGE_SEC", 300, minimum=10, maximum=86_400)
    retry_limit = _positive_int_env("MESSENGER_OUTBOX_READY_MAX_RETRY_AGE_SEC", 1800, minimum=30, maximum=604_800)
    lock_ttl = _positive_int_env("MESSENGER_OUTBOX_LOCK_TTL_SEC", 900, minimum=30, maximum=86_400)
    backlog_limit = _positive_int_env("MESSENGER_OUTBOX_READY_MAX_BACKLOG", 1000, minimum=1, maximum=1_000_000)

    lag_problems: list[str] = []
    if pending_age > pending_limit:
        lag_problems.append(f"oldest_pending_age_sec={pending_age}")
    if retry_age > retry_limit:
        lag_problems.append(f"oldest_retry_age_sec={retry_age}")
    if sending_age > lock_ttl + 60:
        lag_problems.append(f"oldest_sending_age_sec={sending_age}")
    if pending + retry > backlog_limit:
        lag_problems.append(f"delivery_backlog={pending + retry}")

    if worker_expected:
        missing.extend(lag_problems)
    else:
        warnings.extend(f"prestart delivery lag: {problem}" for problem in lag_problems)

    return MessengerPreflightStatus(
        channel="delivery_outbox",
        ok=not missing,
        missing=tuple(missing),
        warnings=tuple(warnings),
        details={"enabled": True, **snapshot},
    )


def check_all_preflights() -> tuple[MessengerPreflightStatus, ...]:
    return (
        check_telegram_preflight(),
        check_max_preflight(),
        check_vk_preflight(),
        check_delivery_outbox_preflight(),
    )

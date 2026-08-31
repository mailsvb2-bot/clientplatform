from __future__ import annotations

"""Production runtime contract checks for Metrotherapy.

This script is intentionally offline: it does not call Telegram, providers, or
external services. It validates the server/process contract that must hold before
ads or live traffic are sent to the bot.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.payment_ingress import resolve_payment_http_enabled


def _truthy(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in {"1", "true", "yes", "on", "webhook"}


def _optional_flag(name: str) -> bool | None:
    if name not in os.environ:
        return None
    return _truthy(name)


def _value(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _first_value(*names: str) -> str:
    for name in names:
        value = _value(name)
        if value:
            return value
    return ""


def _payment_public_base_url() -> str:
    return _first_value("PAYMENT_PUBLIC_BASE_URL", "MESSENGER_PUBLIC_BASE_URL", "PUBLIC_BASE_URL").rstrip("/")


def _privacy_export_public_base_url() -> str:
    return _first_value(
        "PRIVACY_EXPORT_PUBLIC_BASE_URL",
        "MESSENGER_PUBLIC_BASE_URL",
        "PAYMENT_PUBLIC_BASE_URL",
        "PUBLIC_BASE_URL",
    ).rstrip("/")


def _resolved_db_engine() -> str:
    raw = _value("METRO_DB_ENGINE").lower()
    if raw in {"postgres", "postgresql", "pg"}:
        return "postgres"
    if raw in {"sqlite", "sqlite3"}:
        return "sqlite"
    return "postgres" if _value("DATABASE_URL") else "sqlite"


def _is_abs_outside_project(raw: str) -> bool:
    if not raw:
        return False
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            return False
        resolved = path.resolve()
        root = ROOT.resolve()
        return resolved != root and root not in resolved.parents
    except OSError:
        return False


def _positive_int(name: str, default: int, errors: list[str]) -> int:
    raw = _value(name) or str(default)
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name} must be an integer, got {raw!r}")
        return default
    if value <= 0:
        errors.append(f"{name} must be positive, got {value}")
        return default
    return value


def _bounded_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
    errors: list[str],
) -> int:
    value = _positive_int(name, default, errors)
    if value < minimum or value > maximum:
        errors.append(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _payment_enabled() -> bool:
    return resolve_payment_http_enabled(os.environ)

def _privacy_export_enabled() -> bool:
    explicit = _optional_flag("PRIVACY_EXPORT_HTTP_ENABLED")
    return bool(explicit) if explicit is not None else False


def _max_enabled() -> bool:
    explicit = _optional_flag("MAX_WEBHOOK_ENABLED")
    if explicit is not None:
        return explicit
    return _truthy("MESSENGER_WEBHOOK_ENABLED") and bool(_value("MAX_BOT_TOKEN"))


def _vk_enabled() -> bool:
    explicit = _optional_flag("VK_WEBHOOK_ENABLED")
    if explicit is not None:
        return explicit
    return _truthy("MESSENGER_WEBHOOK_ENABLED") and bool(_value("VK_GROUP_TOKEN"))


def _http_ingress_enabled() -> bool:
    return (
        _payment_enabled()
        or _privacy_export_enabled()
        or _max_enabled()
        or _vk_enabled()
    )


def _valid_admin_ids() -> bool:
    raw = _value("ADMIN_IDS") or _value("ADMIN_ID")
    if not raw:
        return False
    tokens = [part.strip() for part in raw.split(",") if part.strip()]
    return bool(tokens) and all(token.isdigit() and int(token) > 0 for token in tokens)


def run() -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    app_env = (_value("APP_ENV") or "dev").lower()
    prod = app_env in {"prod", "production"}
    secure_env = app_env in {"prod", "production", "stage", "staging"}

    transport = (_value("TELEGRAM_TRANSPORT") or _value("RUN_MODE") or "polling").lower()
    if transport != "polling":
        errors.append("TELEGRAM_TRANSPORT must remain polling for this deployment")
    if _truthy("TELEGRAM_WEBHOOK_ENABLED"):
        errors.append("TELEGRAM_WEBHOOK_ENABLED must be 0; Telegram must not be switched to webhook")
    if _truthy("TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED"):
        errors.append("TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED must be 0 in production")
    if _truthy("ALLOW_INSECURE_TELEGRAM_WEBHOOK"):
        errors.append("ALLOW_INSECURE_TELEGRAM_WEBHOOK is forbidden in production")

    if prod:
        for name in ("APP_ENV", "BOT_TOKEN"):
            if not _value(name):
                errors.append(f"{name} is required in prod")
        if not _valid_admin_ids():
            errors.append("ADMIN_IDS or ADMIN_ID must contain positive numeric IDs in prod")

        # Canonical payment path is external YooKassa/package checkout. Validate
        # those credentials only when payment HTTP ingress is active; official
        # owner-control VK/MAX ingress is independent of the payment surface.
        if _payment_enabled():
            for name in ("YOOKASSA_SHOP_ID", "YOOKASSA_SECRET_KEY"):
                if not _value(name):
                    errors.append(f"{name} is required in prod")
            if not _first_value("PAYMENT_CHECKOUT_SIGNING_KEY", "CHECKOUT_SIGNING_KEY"):
                errors.append("PAYMENT_CHECKOUT_SIGNING_KEY is required in prod")
            payment_base = _payment_public_base_url()
            if not payment_base:
                errors.append("PAYMENT_PUBLIC_BASE_URL or MESSENGER_PUBLIC_BASE_URL is required in prod")
            elif not payment_base.startswith("https://"):
                errors.append("payment public base URL must start with https:// in prod")

        if _resolved_db_engine() != "postgres":
            errors.append("METRO_DB_ENGINE must be postgres in prod")
        database_url = _value("DATABASE_URL")
        if not database_url:
            errors.append("DATABASE_URL is required in prod")
        elif not database_url.lower().startswith(("postgresql://", "postgres://")):
            errors.append("DATABASE_URL must use postgres/postgresql scheme in prod")
        if _truthy("ALLOW_SQLITE_IN_PROD"):
            errors.append("ALLOW_SQLITE_IN_PROD is not a supported production bypass")

        log_path = _value("LOG_PATH")
        if not _is_abs_outside_project(log_path):
            errors.append("LOG_PATH must be an absolute path outside the project tree in prod")
        if _truthy("HEALTHCHECK_ENABLED", "1") is False:
            errors.append("HEALTHCHECK_ENABLED must be 1 in prod")
        if not _privacy_export_enabled():
            errors.append("PRIVACY_EXPORT_HTTP_ENABLED must be 1 in prod")

    payment_enabled = _payment_enabled()
    privacy_export_enabled = _privacy_export_enabled()
    max_enabled = _max_enabled()
    vk_enabled = _vk_enabled()
    ingress_enabled = _http_ingress_enabled()

    if payment_enabled and not _payment_public_base_url():
        errors.append("PAYMENT_PUBLIC_BASE_URL or MESSENGER_PUBLIC_BASE_URL is required when payment HTTP ingress is enabled")

    if privacy_export_enabled:
        privacy_base = _privacy_export_public_base_url()
        if not privacy_base:
            errors.append(
                "PRIVACY_EXPORT_PUBLIC_BASE_URL or MESSENGER_PUBLIC_BASE_URL is required "
                "when privacy export HTTP ingress is enabled"
            )
        elif not privacy_base.startswith(("https://", "http://")):
            errors.append("privacy export public base URL must be a full http(s) URL")
        elif secure_env and not privacy_base.startswith("https://"):
            errors.append("privacy export public base URL must start with https:// in secure environments")
        _bounded_int(
            "PRIVACY_EXPORT_TOKEN_TTL_MINUTES",
            10,
            minimum=2,
            maximum=30,
            errors=errors,
        )

    public_base = _value("MESSENGER_PUBLIC_BASE_URL")
    if max_enabled or vk_enabled:
        if not public_base:
            errors.append("MESSENGER_PUBLIC_BASE_URL is required when MAX/VK webhook ingress is enabled")
        elif prod and not public_base.startswith("https://"):
            errors.append("MESSENGER_PUBLIC_BASE_URL must start with https:// in prod")

    if max_enabled:
        for name in ("MAX_BOT_TOKEN", "MAX_BOT_LINK_BASE"):
            if not _value(name):
                errors.append(f"{name} is required when MAX webhook ingress is enabled")
        if prod and not _value("MAX_WEBHOOK_SECRET"):
            errors.append("MAX_WEBHOOK_SECRET is required in prod when MAX webhook ingress is enabled")

    if vk_enabled:
        for name in ("VK_GROUP_TOKEN", "VK_CONFIRMATION_TOKEN", "VK_GROUP_ID"):
            if not _value(name):
                errors.append(f"{name} is required when VK webhook ingress is enabled")
        if prod and not _value("VK_SECRET"):
            errors.append("VK_SECRET is required in prod when VK webhook ingress is enabled")

    if ingress_enabled:
        ingress_host = _value("MESSENGER_WEBHOOK_HOST") or _value("WEBHOOK_HOST") or "127.0.0.1"
        ingress_port = _positive_int(
            "MESSENGER_WEBHOOK_PORT" if _value("MESSENGER_WEBHOOK_PORT") else "WEBHOOK_PORT",
            8081,
            errors,
        )
        health_host = _value("HEALTHCHECK_HOST") or "127.0.0.1"
        health_port = _positive_int("HEALTHCHECK_PORT", 8082, errors)
        same_host = ingress_host == health_host or "0.0.0.0" in {ingress_host, health_host}  # nosec B104 - sentinel comparison, not a bind
        if same_host and ingress_port == health_port:
            errors.append(f"HTTP ingress port and health port collide on {ingress_host}:{ingress_port}")
    else:
        warnings.append(
            "HTTP ingress is disabled; YooKassa/privacy export/MAX/VK web endpoints "
            "will not be served by this process"
        )

    return errors, warnings


def main() -> int:
    errors, warnings = run()
    for warning in warnings:
        print(f"WARN: {warning}")
    if errors:
        print("RUNTIME CONTRACT: FAILED")
        for error in errors:
            print(f"ERROR: {error}")
        return 2
    print("RUNTIME CONTRACT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

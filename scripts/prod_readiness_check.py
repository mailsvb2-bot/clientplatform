from __future__ import annotations

"""Offline/live production-readiness checks.

This script deliberately does not call Telegram/YooKassa/VK/MAX. It validates
runtime contracts before deploy or during post-deploy smoke: required production
configuration, split HTTP ingress, port isolation and required runtime paths.

Release artifact checks are strict only when VALIDATOR_RELEASE_MODE=1 or
PROD_READINESS_RELEASE_MODE=1. A live server may contain runtime files; those are
warnings in normal mode and errors in release packaging mode.
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.payment_ingress import resolve_payment_http_enabled

SKIP_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}


def _truthy(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in {"1", "true", "yes", "on", "webhook"}


def _optional_flag(name: str) -> bool | None:
    if name not in os.environ:
        return None
    return _truthy(name)


def _int(name: str, default: int, errors: list[str]) -> int:
    raw = (os.getenv(name, str(default)) or str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name} must be integer, got {raw!r}")
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
    value = _int(name, default, errors)
    if value < minimum or value > maximum:
        errors.append(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _int_fallback(name: str, fallback_name: str, default: int, errors: list[str]) -> int:
    if (os.getenv(name) or "").strip():
        return _int(name, default, errors)
    return _int(fallback_name, default, errors)


def _looks_placeholder(value: str) -> bool:
    return value.strip().upper().startswith("PASTE_") or value.strip() in {"", "x", "y", "changeme", "secret"}


def _release_mode() -> bool:
    return _truthy("VALIDATOR_RELEASE_MODE") or _truthy("PROD_READINESS_RELEASE_MODE")


def _require_env(name: str, errors: list[str], *, placeholder: bool = True) -> str:
    value = (os.getenv(name) or "").strip()
    if not value or (placeholder and _looks_placeholder(value)):
        errors.append(f"{name} is missing or placeholder")
    return value


def _first_env(*names: str) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _payment_public_base_url() -> str:
    return _first_env("PAYMENT_PUBLIC_BASE_URL", "MESSENGER_PUBLIC_BASE_URL", "PUBLIC_BASE_URL").rstrip("/")


def _privacy_export_public_base_url() -> str:
    return _first_env(
        "PRIVACY_EXPORT_PUBLIC_BASE_URL",
        "MESSENGER_PUBLIC_BASE_URL",
        "PAYMENT_PUBLIC_BASE_URL",
        "PUBLIC_BASE_URL",
    ).rstrip("/")


def _payment_http_enabled() -> bool:
    return resolve_payment_http_enabled(os.environ)

def _privacy_export_http_enabled() -> bool:
    explicit = _optional_flag("PRIVACY_EXPORT_HTTP_ENABLED")
    return bool(explicit) if explicit is not None else False


def _max_webhook_enabled() -> bool:
    explicit = _optional_flag("MAX_WEBHOOK_ENABLED")
    if explicit is not None:
        return explicit
    return _truthy("MESSENGER_WEBHOOK_ENABLED") and bool((os.getenv("MAX_BOT_TOKEN") or "").strip())


def _vk_webhook_enabled() -> bool:
    explicit = _optional_flag("VK_WEBHOOK_ENABLED")
    if explicit is not None:
        return explicit
    return _truthy("MESSENGER_WEBHOOK_ENABLED") and bool((os.getenv("VK_GROUP_TOKEN") or "").strip())


def _http_ingress_enabled() -> bool:
    return (
        _payment_http_enabled()
        or _privacy_export_http_enabled()
        or _max_webhook_enabled()
        or _vk_webhook_enabled()
    )


def _validate_admin_ids(errors: list[str]) -> None:
    raw = (os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID") or "").strip()
    if _looks_placeholder(raw):
        errors.append("ADMIN_IDS or ADMIN_ID is required in prod")
        return
    tokens = [part.strip() for part in raw.split(",") if part.strip()]
    invalid = [token for token in tokens if not token.isdigit() or int(token) <= 0]
    if invalid or not tokens:
        errors.append("ADMIN_IDS or ADMIN_ID must contain positive numeric IDs")


def _validate_ai_runtime(prod: bool, errors: list[str], warnings: list[str]) -> None:
    enabled_raw = (os.getenv("AI_ENABLED") or "1").strip().lower()
    ai_enabled = enabled_raw not in {"0", "false", "no", "off"}
    provider = (os.getenv("AI_PROVIDER") or "").strip().lower()

    has_openai = bool((os.getenv("OPENAI_API_KEY") or "").strip())
    has_yandex = bool((os.getenv("YANDEX_API_KEY") or "").strip())
    has_gigachat = bool((os.getenv("GIGACHAT_CREDENTIALS") or "").strip())
    configured = has_openai or has_yandex or has_gigachat

    if not ai_enabled:
        return
    if not configured:
        warnings.append("AI_ENABLED is on, but no AI provider credentials are configured; admin AI surfaces will be disabled/degraded")
        return
    if provider and provider not in {"openai", "openai-compatible", "openai_compatible", "compatible", "yandex", "gigachat", "sber"}:
        errors.append(f"AI_PROVIDER has unsupported value: {provider!r}")
    if prod and provider == "yandex" and has_yandex and not (os.getenv("YANDEX_FOLDER_ID") or "").strip():
        warnings.append("YANDEX_API_KEY is set but YANDEX_FOLDER_ID is empty; default model resolution may be incomplete")


def _validate_database_runtime(prod: bool, errors: list[str], warnings: list[str]) -> None:
    database_url = (os.getenv("DATABASE_URL") or "").strip()
    engine = (os.getenv("METRO_DB_ENGINE") or "").strip().lower()
    if prod:
        if engine not in {"postgres", "postgresql", "pg"}:
            errors.append("METRO_DB_ENGINE must be postgres in prod")
        if not database_url:
            errors.append("DATABASE_URL is required in prod")
        elif not database_url.startswith(("postgresql://", "postgres://")):
            errors.append("DATABASE_URL must use postgres/postgresql scheme in prod")
        if _truthy("ALLOW_SQLITE_IN_PROD"):
            errors.append("ALLOW_SQLITE_IN_PROD is not a supported production bypass")
        return

    if database_url and not database_url.startswith(("postgresql://", "postgres://")):
        warnings.append("DATABASE_URL is set but does not look like a Postgres URL")


def _validate_payment_runtime(prod: bool, errors: list[str]) -> None:
    if not prod:
        return
    if _payment_http_enabled():
        for name in ("YOOKASSA_SHOP_ID", "YOOKASSA_SECRET_KEY"):
            _require_env(name, errors)
        if not _first_env("PAYMENT_CHECKOUT_SIGNING_KEY", "CHECKOUT_SIGNING_KEY"):
            errors.append("PAYMENT_CHECKOUT_SIGNING_KEY is missing or placeholder")
        public_base = _payment_public_base_url()
        if not public_base:
            errors.append("PAYMENT_PUBLIC_BASE_URL or MESSENGER_PUBLIC_BASE_URL is missing or placeholder")
        elif not public_base.startswith("https://"):
            errors.append("payment public base URL must start with https:// in prod")
    dangerous = [
        "ALLOW_UNSIGNED_PAYMENT_CHECKOUT_IN_PROD",
        "ALLOW_UNVERIFIED_YOOKASSA_WEBHOOK_IN_PROD",
        "ALLOW_STATIC_PAYMENT_IDEMPOTENCE_KEY_IN_PROD",
    ]
    if not _truthy("PAYMENT_DANGEROUS_OVERRIDES_ALLOWED"):
        enabled = [name for name in dangerous if _truthy(name)]
        if enabled:
            errors.append("dangerous payment override(s) enabled in prod: " + ", ".join(enabled))


def _validate_http_ingress(prod: bool, errors: list[str], warnings: list[str]) -> bool:
    payment_enabled = _payment_http_enabled()
    privacy_export_enabled = _privacy_export_http_enabled()
    max_enabled = _max_webhook_enabled()
    vk_enabled = _vk_webhook_enabled()
    ingress_enabled = payment_enabled or privacy_export_enabled or max_enabled or vk_enabled

    if payment_enabled and not _payment_public_base_url():
        errors.append("PAYMENT_PUBLIC_BASE_URL or MESSENGER_PUBLIC_BASE_URL is required when payment HTTP ingress is enabled")

    if prod and not privacy_export_enabled:
        errors.append("PRIVACY_EXPORT_HTTP_ENABLED must be 1 in prod")
    if privacy_export_enabled:
        privacy_base = _privacy_export_public_base_url()
        if not privacy_base:
            errors.append(
                "PRIVACY_EXPORT_PUBLIC_BASE_URL or MESSENGER_PUBLIC_BASE_URL is required "
                "when privacy export HTTP ingress is enabled"
            )
        elif not privacy_base.startswith(("https://", "http://")):
            errors.append("privacy export public base URL must be a full http(s) URL")
        elif prod and not privacy_base.startswith("https://"):
            errors.append("privacy export public base URL must start with https:// in prod")
        _bounded_int(
            "PRIVACY_EXPORT_TOKEN_TTL_MINUTES",
            10,
            minimum=2,
            maximum=30,
            errors=errors,
        )

    public_base = (os.getenv("MESSENGER_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if max_enabled or vk_enabled:
        if not public_base:
            errors.append("MESSENGER_PUBLIC_BASE_URL is required for MAX/VK webhook runtime")
        elif prod and not public_base.startswith("https://"):
            errors.append("MESSENGER_PUBLIC_BASE_URL must start with https:// in prod")
        elif not (public_base.startswith("https://") or public_base.startswith("http://")):
            warnings.append("MESSENGER_PUBLIC_BASE_URL should be a full URL, for example https://your-domain.tld")

    if max_enabled:
        max_link_base = _require_env("MAX_BOT_LINK_BASE", errors, placeholder=False)
        _require_env("MAX_BOT_TOKEN", errors)
        if max_link_base and "{payload}" not in max_link_base:
            warnings.append("MAX_BOT_LINK_BASE has no {payload}; fallback ?start=... links may be less reliable")
        if prod:
            _require_env("MAX_WEBHOOK_SECRET", errors)

    if vk_enabled:
        _require_env("VK_GROUP_ID", errors)
        _require_env("VK_GROUP_TOKEN", errors)
        _require_env("VK_CONFIRMATION_TOKEN", errors)
        if prod:
            _require_env("VK_SECRET", errors)
        elif not (os.getenv("VK_SECRET") or "").strip():
            warnings.append("VK_SECRET is empty; VK webhook secret verification is not enforced")

    if not ingress_enabled:
        warnings.append(
            "HTTP ingress is disabled; YooKassa/privacy export/MAX/VK web endpoints "
            "will not be served by this process"
        )
    return ingress_enabled


def _is_runtime_scan_candidate(path: Path) -> bool:
    try:
        rel_parts = path.relative_to(ROOT).parts
    except ValueError:
        return False
    return not any(part in SKIP_DIRS for part in rel_parts)


def _collect_release_artifacts() -> list[str]:
    forbidden: list[str] = []
    secret_patterns = (
        re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{25,}\b"),
        re.compile(r"live_[A-Za-z0-9_-]{16,}"),
    )
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel_parts = p.relative_to(ROOT).parts
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        if p.suffix.lower() in {".opus", ".ogg", ".mp3", ".wav", ".m4a", ".png", ".jpg", ".jpeg", ".zip", ".so"}:
            continue
        if p.name.startswith(".env"):
            forbidden.append(str(p.relative_to(ROOT)))
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(rx.search(txt) for rx in secret_patterns):
            forbidden.append(f"embedded-secret:{p.relative_to(ROOT)}")

    forbidden.extend(
        str(p.relative_to(ROOT))
        for p in ROOT.rglob("__pycache__")
        if p.is_dir() and _is_runtime_scan_candidate(p)
    )
    forbidden.extend(
        str(p.relative_to(ROOT))
        for p in ROOT.rglob("*.pyc")
        if p.is_file() and _is_runtime_scan_candidate(p)
    )
    forbidden.extend(
        str(p.relative_to(ROOT))
        for p in [ROOT / ".pytest_cache", ROOT / "data.db", ROOT / "data" / "data.db"]
        if p.exists()
    )
    forbidden.extend(
        str(p.relative_to(ROOT))
        for p in ROOT.rglob("*.db-wal")
        if p.is_file() and _is_runtime_scan_candidate(p)
    )
    forbidden.extend(
        str(p.relative_to(ROOT))
        for p in ROOT.rglob("*.db-shm")
        if p.is_file() and _is_runtime_scan_candidate(p)
    )
    forbidden.extend(
        str(p.relative_to(ROOT))
        for p in ROOT.rglob("*.log")
        if p.is_file() and _is_runtime_scan_candidate(p)
    )
    return sorted(set(forbidden))


def run() -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    app_env = (os.getenv("APP_ENV", "dev") or "dev").strip().lower()
    prod = app_env in {"prod", "production"}
    bot_token = (os.getenv("BOT_TOKEN") or "").strip()

    if prod:
        if _looks_placeholder(bot_token):
            errors.append("BOT_TOKEN is missing or placeholder")
        if bot_token and not re.match(r"^\d{8,12}:[A-Za-z0-9_-]{25,}$", bot_token):
            warnings.append("BOT_TOKEN format does not look like a Telegram bot token")
        _validate_admin_ids(errors)

    _validate_payment_runtime(prod, errors)
    _validate_database_runtime(prod, errors, warnings)
    _validate_ai_runtime(prod, errors, warnings)

    telegram_transport = (os.getenv("TELEGRAM_TRANSPORT", os.getenv("RUN_MODE", "polling")) or "polling").strip().lower()
    telegram_webhook = telegram_transport == "webhook" or _truthy("TELEGRAM_WEBHOOK_ENABLED")
    http_ingress = _validate_http_ingress(prod, errors, warnings)

    if prod and not _truthy("HEALTHCHECK_ENABLED", "1"):
        errors.append("HEALTHCHECK_ENABLED must be 1 in prod")

    if telegram_webhook:
        public_base = (os.getenv("TELEGRAM_WEBHOOK_PUBLIC_BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "").strip()
        if not public_base:
            errors.append("TELEGRAM_WEBHOOK_PUBLIC_BASE_URL is required for Telegram webhook mode")
        elif prod and not public_base.startswith("https://"):
            errors.append("TELEGRAM_WEBHOOK_PUBLIC_BASE_URL must start with https:// in prod")
        prefix = (os.getenv("TELEGRAM_WEBHOOK_PREFIX", "/telegram-webhook") or "/telegram-webhook").strip()
        if not prefix.startswith("/"):
            errors.append("TELEGRAM_WEBHOOK_PREFIX must start with /")
        if prod and not (os.getenv("TELEGRAM_WEBHOOK_SECRET_TOKEN") or "").strip():
            errors.append("TELEGRAM_WEBHOOK_SECRET_TOKEN is required in prod webhook mode")

    if telegram_webhook or http_ingress:
        if telegram_webhook:
            ingress_host = (os.getenv("TELEGRAM_WEBHOOK_HOST") or os.getenv("WEBHOOK_HOST") or "127.0.0.1").strip()
            ingress_port = _int_fallback("TELEGRAM_WEBHOOK_PORT", "WEBHOOK_PORT", 8081, errors)
        else:
            ingress_host = (os.getenv("MESSENGER_WEBHOOK_HOST") or os.getenv("WEBHOOK_HOST") or "127.0.0.1").strip()
            ingress_port = _int_fallback("MESSENGER_WEBHOOK_PORT", "WEBHOOK_PORT", 8081, errors)
        health_host = (os.getenv("HEALTHCHECK_HOST", "127.0.0.1") or "127.0.0.1").strip()
        health_port = _int("HEALTHCHECK_PORT", 8082, errors)
        same_host = ingress_host == health_host or "0.0.0.0" in {ingress_host, health_host}  # nosec B104 - sentinel comparison, not a bind
        if same_host and ingress_port == health_port:
            errors.append(f"HTTP ingress port and health port collide on {ingress_host}:{ingress_port}")

    required_paths = [ROOT / "audio" / "demo", ROOT / "audio" / "full", ROOT / "data"]
    for path in required_paths:
        if not path.exists():
            errors.append(f"Required path missing: {path.relative_to(ROOT)}")

    forbidden = _collect_release_artifacts()
    if forbidden:
        message = "Forbidden release/runtime artifacts present: " + ", ".join(forbidden[:30])
        if _release_mode():
            errors.append(message)
        else:
            warnings.append(message + " (strict only in release mode)")

    return errors, warnings


def main() -> int:
    errors, warnings = run()
    for warning in warnings:
        print(f"WARN: {warning}")
    if errors:
        print("PROD READINESS: FAILED")
        for error in errors:
            print(f"ERROR: {error}")
        return 2
    print("PROD READINESS: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import os

from services.validators.base import ValidationError

_POLLING_ALIASES = {"polling", "telegram", "longpoll", "long-polling"}
_TRUE_VALUES = {"1", "true", "yes", "on", "webhook"}
_DISABLED_VALUES = {"0", "false", "no", "off"}



def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value is not None else default


def _truthy(name: str, default: str = "0") -> bool:
    return (_env(name, default) or default).strip().lower() in _TRUE_VALUES


def _explicitly_disabled(name: str) -> bool:
    raw = os.getenv(name)
    return raw is not None and str(raw).strip().lower() in _DISABLED_VALUES


def _prod() -> bool:
    return (_env("APP_ENV", "dev") or "dev").strip().lower() in {"prod", "production"}


def _first_env(*names: str) -> str:
    for name in names:
        value = _env(name)
        if value:
            return value
    return ""


def _resolved_db_engine() -> str:
    raw = (_env("CLIENTPLATFORM_DB_ENGINE") or "").strip().lower()
    if raw in {"postgres", "postgresql", "pg"}:
        return "postgres"
    if raw in {"sqlite", "sqlite3"}:
        return "sqlite"
    return "postgres" if _env("DATABASE_URL") else "sqlite"



def validate_prod_admin_contract(*, strict: bool = True) -> None:
    """Production must have at least one actually parseable positive admin ID."""

    if not _prod():
        return

    raw_many = _env("ADMIN_IDS")
    raw_one = _env("ADMIN_ID")
    if raw_many:
        tokens = [part.strip() for part in raw_many.split(",") if part.strip()]
        source_name = "ADMIN_IDS"
    elif raw_one:
        tokens = [raw_one]
        source_name = "ADMIN_ID"
    else:
        tokens = []
        source_name = "ADMIN_IDS/ADMIN_ID"

    invalid = [token for token in tokens if not token.isdigit() or int(token) <= 0]
    valid = [int(token) for token in tokens if token.isdigit() and int(token) > 0]
    errors: list[str] = []
    if invalid:
        errors.append(f"{source_name} contains invalid values: {', '.join(invalid)}")
    if not valid:
        errors.append("at least one positive numeric admin id is required")

    if errors and strict:
        raise ValidationError("Production admin contract failed: " + "; ".join(errors))


def validate_prod_telegram_polling_contract(*, strict: bool = True) -> None:
    """Production Telegram ingress is polling-only.

    Telegram webhook code remains a dev/migration capability, but production must
    not silently switch away from polling. MAX/VK and privacy export may still use the local
    aiohttp messenger runtime; this check is only about Telegram updates.
    """

    if not _prod():
        return

    transport = (_env("TELEGRAM_TRANSPORT") or _env("RUN_MODE") or "polling").strip().lower()
    errors: list[str] = []
    if transport not in _POLLING_ALIASES:
        errors.append("TELEGRAM_TRANSPORT must be polling in prod")
    if _truthy("TELEGRAM_WEBHOOK_ENABLED"):
        errors.append("TELEGRAM_WEBHOOK_ENABLED must be 0 in prod; Telegram ingress is polling-only")
    if _truthy("TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED"):
        errors.append("TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED must be 0 in prod")
    if _truthy("ALLOW_INSECURE_TELEGRAM_WEBHOOK"):
        errors.append("ALLOW_INSECURE_TELEGRAM_WEBHOOK is forbidden in prod")

    if errors and strict:
        raise ValidationError("Production Telegram polling contract failed: " + "; ".join(errors))


def validate_prod_postgres_contract(*, strict: bool = True) -> None:
    """Production storage is Postgres-only.

    SQLite remains available for local/dev and hermetic tests. In production it is
    forbidden as an active engine because scheduler locks, payment idempotency,
    backup/restore drills and horizontal recovery must share one durable source
    of truth.
    """

    if not _prod():
        return

    errors: list[str] = []
    engine = _resolved_db_engine()
    database_url = _env("DATABASE_URL")
    if engine != "postgres":
        errors.append("CLIENTPLATFORM_DB_ENGINE must be postgres in prod")
    if not database_url:
        errors.append("DATABASE_URL is required in prod")
    elif not database_url.lower().startswith(("postgresql://", "postgres://")):
        errors.append("DATABASE_URL must use postgres/postgresql scheme in prod")
    if _truthy("ALLOW_SQLITE_IN_PROD"):
        errors.append("ALLOW_SQLITE_IN_PROD is not a supported production bypass")

    if errors and strict:
        raise ValidationError("Production Postgres contract failed: " + "; ".join(errors))


def validate_prod_guardrails(*, strict: bool = True) -> None:
    """Fail closed when production starts without release architecture guardrails.

    The app already has a production config fail-fast, but release validation and
    architecture checks used to depend on optional environment flags. In prod this
    must be an explicit deployment contract, not a README recommendation.
    """

    app_env = (os.getenv("APP_ENV", "dev") or "dev").strip().lower()
    if app_env not in {"prod", "production"}:
        return

    validate_prod_admin_contract(strict=True)
    validate_prod_telegram_polling_contract(strict=True)
    validate_prod_postgres_contract(strict=True)

    if os.getenv("ALLOW_UNGUARDED_PROD", "").strip().lower() in {"1", "true", "yes", "on"}:
        raise ValidationError("ALLOW_UNGUARDED_PROD is forbidden in prod")

    missing: list[str] = []
    if os.getenv("VALIDATOR_RELEASE_MODE", "").strip().lower() not in {"1", "true", "yes", "on"}:
        missing.append("VALIDATOR_RELEASE_MODE=1")
    if os.getenv("VALIDATOR_GUARDRAILS_STRICT", "").strip().lower() not in {"1", "true", "yes", "on"}:
        missing.append("VALIDATOR_GUARDRAILS_STRICT=1")

    if missing:
        msg = "Production requires release guardrails: " + ", ".join(missing)
        if strict:
            raise ValidationError(msg)

from __future__ import annotations

import os

from core.environment import is_production_env


class ProductionContractError(RuntimeError):
    pass


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _truthy(name: str, default: str = "0") -> bool:
    return (_env(name) or default).strip().lower() in {"1", "true", "yes", "on", "webhook"}


def _db_engine() -> str:
    raw = _env("CLIENTPLATFORM_DB_ENGINE").lower()
    if raw in {"postgres", "postgresql", "pg"}:
        return "postgres"
    if raw in {"sqlite", "sqlite3"}:
        return "sqlite"
    return "postgres" if _env("DATABASE_URL") else "sqlite"


def validate_production_contract() -> None:
    if not is_production_env():
        return

    problems: list[str] = []

    transport = (_env("TELEGRAM_TRANSPORT") or _env("RUN_MODE") or "polling").lower()
    if transport != "polling":
        problems.append("TELEGRAM_TRANSPORT must be polling in production")
    if _truthy("TELEGRAM_WEBHOOK_ENABLED"):
        problems.append("TELEGRAM_WEBHOOK_ENABLED must be 0 in production")
    if _truthy("TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED"):
        problems.append("TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED must be 0 in production")
    if _truthy("ALLOW_INSECURE_TELEGRAM_WEBHOOK"):
        problems.append("ALLOW_INSECURE_TELEGRAM_WEBHOOK is forbidden in production")

    if _db_engine() != "postgres":
        problems.append("CLIENTPLATFORM_DB_ENGINE must be postgres in production")
    database_url = _env("DATABASE_URL")
    if not database_url:
        problems.append("DATABASE_URL is required in production")
    elif not database_url.lower().startswith(("postgresql://", "postgres://")):
        problems.append("DATABASE_URL must use postgres/postgresql scheme in production")
    if _truthy("ALLOW_SQLITE_IN_PROD"):
        problems.append("ALLOW_SQLITE_IN_PROD is not a supported production bypass")

    if problems:
        raise ProductionContractError("Production contract failed: " + "; ".join(problems))

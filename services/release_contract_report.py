from __future__ import annotations

import os

from services.db.runtime import CONFIG, redacted_db_target
from services.disaster_recovery_status import disaster_recovery_status
from services.storage_legacy_audit import storage_legacy_audit

_TRUE_VALUES = {"1", "true", "yes", "on", "webhook"}


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value is not None else default


def _truthy(name: str, default: str = "0") -> bool:
    return (_env(name, default) or default).strip().lower() in _TRUE_VALUES


def _first_env(*names: str) -> str:
    for name in names:
        value = _env(name)
        if value:
            return value
    return ""


def _telegram_transport() -> str:
    return (_env("TELEGRAM_TRANSPORT") or _env("RUN_MODE") or "polling").strip().lower()


def format_runtime_contract_report() -> str:
    """Return an admin-facing summary of the hard production contracts.

    This is intentionally read-only: no probes, no network calls, no mutations.
    It surfaces the exact contracts that previously drifted silently: Postgres as
    active storage, Telegram polling-only, backup freshness, legacy SQLite and
    channel and recovery fail-closed settings.
    """
    storage = storage_legacy_audit()
    disaster = disaster_recovery_status(include_hash=False)
    telegram_transport = _telegram_transport()
    telegram_webhook_enabled = _truthy("TELEGRAM_WEBHOOK_ENABLED")


    postgres_ok = CONFIG.engine == "postgres" and bool(storage.database_url_configured)
    telegram_ok = telegram_transport == "polling" and not telegram_webhook_enabled
    legacy_ok = not bool(storage.legacy_sqlite_present) and not bool(storage.repo_local_sqlite_present)
    backup_ok = disaster.status == "GREEN"
    marker = "✅" if postgres_ok and telegram_ok and legacy_ok and backup_ok else "⚠️"

    lines = [
        "🔒 Production runtime contract",
        "",
        f"Статус: {marker}",
        f"DB engine: {CONFIG.engine}",
        f"DB target: {redacted_db_target()}",
        f"DATABASE_URL configured: {storage.database_url_configured}",
        f"Telegram transport: {telegram_transport}",
        f"Telegram webhook enabled: {telegram_webhook_enabled}",
        f"Legacy SQLite present: {storage.legacy_sqlite_present}",
        f"Repo SQLite present: {storage.repo_local_sqlite_present}",
        f"Disallowed sqlite3.connect points: {len(storage.disallowed_direct_sqlite_connects)}",
        f"Backup status: {disaster.status}",
        f"Backup fresh: {disaster.latest_backup_fresh}",
        f"Backup age seconds: {disaster.latest_backup_age_seconds if disaster.latest_backup_age_seconds is not None else '-'}",
        f"Max backup age hours: {disaster.max_backup_age_hours:g}",
        f"Restore target configured: {disaster.restore_target_configured}",
    ]

    if postgres_ok and telegram_ok and legacy_ok and backup_ok:
        lines.append("\nИтог: production-контракт полностью зелёный.")
    else:
        lines.append("\nИтог: есть пункт для operator cleanup/check; production gate решает, блокер это или нет.")
    return "\n".join(lines)


__all__ = ["format_runtime_contract_report"]

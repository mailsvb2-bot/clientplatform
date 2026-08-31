from __future__ import annotations

import os

from core.payment_ingress import resolve_payment_http_enabled
from services.db.runtime import CONFIG, redacted_db_target
from services.disaster_recovery_status import disaster_recovery_status
from services.storage_legacy_audit import storage_legacy_audit

_TRUE_VALUES = {"1", "true", "yes", "on", "webhook"}
_HARD_TOKEN_VALUES = {"hard", "1", "true", "yes", "on"}
_DISABLED_VALUES = {"0", "false", "no", "off"}


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
    production monetization fail-closed settings.
    """
    storage = storage_legacy_audit()
    disaster = disaster_recovery_status(include_hash=False)
    telegram_transport = _telegram_transport()
    telegram_webhook_enabled = _truthy("TELEGRAM_WEBHOOK_ENABLED")

    token_economy_enabled = _env("TOKEN_ECONOMY_ENABLED", "1").strip().lower() not in _DISABLED_VALUES
    token_enforcement_mode = _env("TOKEN_ENFORCEMENT_MODE")
    payment_http_enabled = resolve_payment_http_enabled(os.environ)
    receipt_contact_configured = bool(_first_env("YOOKASSA_RECEIPT_EMAIL", "PAYMENT_RECEIPT_EMAIL", "ADMIN_EMAIL"))

    postgres_ok = CONFIG.engine == "postgres" and bool(storage.database_url_configured)
    telegram_ok = telegram_transport == "polling" and not telegram_webhook_enabled
    legacy_ok = not bool(storage.legacy_sqlite_present) and not bool(storage.repo_local_sqlite_present)
    backup_ok = disaster.status == "GREEN"
    monetization_ok = (
        token_economy_enabled
        and token_enforcement_mode.strip().lower() in _HARD_TOKEN_VALUES
        and (receipt_contact_configured or not payment_http_enabled)
    )
    marker = "✅" if postgres_ok and telegram_ok and legacy_ok and backup_ok and monetization_ok else "⚠️"

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
        f"Token economy enabled: {token_economy_enabled}",
        f"Token enforcement mode: {token_enforcement_mode or '<missing>'}",
        f"Payment HTTP enabled: {payment_http_enabled}",
        f"Receipt contact configured: {receipt_contact_configured}",
    ]

    if postgres_ok and telegram_ok and legacy_ok and backup_ok and monetization_ok:
        lines.append("\nИтог: production-контракт полностью зелёный.")
    else:
        lines.append("\nИтог: есть пункт для operator cleanup/check; production gate решает, блокер это или нет.")
    return "\n".join(lines)


__all__ = ["format_runtime_contract_report"]

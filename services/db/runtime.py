from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from core.paths import DB_PATH, DATABASE_URL, DB_ENGINE


@dataclass(frozen=True)
class DbRuntimeConfig:
    engine: str
    db_path: Path
    database_url: str

    @property
    def uses_postgres(self) -> bool:
        return self.engine == "postgres"

    @property
    def uses_sqlite(self) -> bool:
        return self.engine == "sqlite"


def _normalize_engine(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in {"postgres", "postgresql", "pg"}:
        return "postgres"
    return "sqlite"


def _timeout_seconds(name: str, default: float, *, minimum: float) -> float:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    return value if value >= minimum else float(default)


def _strip_pg_option(options: str, setting: str) -> str:
    pattern = re.compile(
        rf"(?:^|\s)-c\s+{re.escape(setting)}(?:\s*=\s*|\s+)[^\s]+",
        re.IGNORECASE,
    )
    return " ".join(pattern.sub(" ", str(options or "")).split())


def configure_libpq_timeouts() -> None:
    """Apply bounded waits to every psycopg/libpq connection in this process.

    libpq reads ``PGCONNECT_TIMEOUT`` and ``PGOPTIONS`` for connections whose
    DSN does not provide stricter values. We append the canonical settings last
    so a stale process environment cannot silently re-enable unbounded waits.
    """

    connect_timeout = max(
        1,
        int(_timeout_seconds("POSTGRES_CONNECT_TIMEOUT_SEC", 5.0, minimum=1.0) + 0.999),
    )
    timeout_ms = {
        "statement_timeout": max(
            1,
            int(_timeout_seconds("POSTGRES_STATEMENT_TIMEOUT_SEC", 15.0, minimum=0.1) * 1000),
        ),
        "lock_timeout": max(
            1,
            int(_timeout_seconds("POSTGRES_LOCK_TIMEOUT_SEC", 3.0, minimum=0.1) * 1000),
        ),
        "idle_in_transaction_session_timeout": max(
            1,
            int(_timeout_seconds("POSTGRES_IDLE_TX_TIMEOUT_SEC", 30.0, minimum=0.1) * 1000),
        ),
    }

    os.environ["PGCONNECT_TIMEOUT"] = str(connect_timeout)
    options = os.getenv("PGOPTIONS", "")
    for setting in timeout_ms:
        options = _strip_pg_option(options, setting)
    enforced = " ".join(
        f"-c {setting}={value}ms" for setting, value in timeout_ms.items()
    )
    os.environ["PGOPTIONS"] = " ".join(part for part in (options, enforced) if part).strip()


CONFIG: Final[DbRuntimeConfig] = DbRuntimeConfig(
    engine=_normalize_engine(DB_ENGINE),
    db_path=Path(DB_PATH),
    database_url=(DATABASE_URL or "").strip(),
)

if CONFIG.uses_postgres:
    configure_libpq_timeouts()


def is_postgres_enabled() -> bool:
    return CONFIG.uses_postgres


def is_sqlite_enabled() -> bool:
    return CONFIG.uses_sqlite


def redacted_db_target() -> str:
    if CONFIG.uses_sqlite:
        return str(CONFIG.db_path)
    url = CONFIG.database_url
    if not url:
        return "postgres://<missing>"
    if "@" not in url:
        return url
    prefix, suffix = url.split("@", 1)
    if "://" in prefix:
        scheme, creds = prefix.split("://", 1)
        if ":" in creds:
            user, _ = creds.split(":", 1)
            return f"{scheme}://{user}:***@{suffix}"
        return f"{scheme}://***@{suffix}"
    return f"***@{suffix}"


def postgres_driver_error_hint() -> str:
    return (
        "Postgres mode requires psycopg. Install dependencies from requirements.txt "
        "and set DATABASE_URL, for example: "
        "postgresql://clientplatform:secret@127.0.0.1:5432/clientplatform"
    )

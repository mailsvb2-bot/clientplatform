from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable

from clientplatform.runtime.dispatch_runtime import (
    DispatchRuntimeConfig,
    build_dispatch_runtime,
    dispatch_runtime_config,
)
from clientplatform.runtime.lifecycle import (
    start_clientplatform_runtime,
    stop_clientplatform_runtime,
)
from core.runtime_env import env_float
from services.db import get_connection
from services.db.runtime import CONFIG

log = logging.getLogger(__name__)

_CLIENTPLATFORM_REQUIRED_TABLES = frozenset(
    {
        "accounts",
        "account_channel_identities",
        "businesses",
        "business_members",
        "customers",
        "customer_identities",
        "business_profiles",
        "business_capabilities",
        "business_offerings",
        "customer_invites",
        "booking_slots",
        "programs",
        "lessons",
        "enrollments",
        "lesson_deliveries",
        "lesson_progress",
        "connections",
        "connection_credentials",
        "messenger_ingress_routes",
        "messenger_connection_setup_sessions",
        "customer_channel_link_tokens",
        "managed_bots",
        "delivery_dispatch_outbox",
        "provider_dispatch_outbox",
    }
)

SchemaProbe = Callable[[], tuple[bool, str | None]]
Sleep = Callable[[float], Awaitable[None]]
Monotonic = Callable[[], float]


def _schema_error(exc: BaseException) -> tuple[bool, str]:
    return False, f"clientplatform_schema:{type(exc).__name__}"


def _clientplatform_schema_readiness() -> tuple[bool, str | None]:
    """Verify the complete additive clientplatform dispatch schema before starting workers."""

    try:
        with get_connection() as conn:
            if CONFIG.uses_postgres:
                rows = conn.execute(
                    "SELECT table_name AS name FROM information_schema.tables "
                    "WHERE table_schema=current_schema()",
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'",
                ).fetchall()

        names: set[str] = set()
        for row in rows:
            if isinstance(row, dict):
                value = row.get("table_name") or row.get("name")
            else:
                try:
                    value = row[0]
                except (IndexError, KeyError, TypeError):
                    value = None
            if value:
                names.add(str(value))

        missing = sorted(_CLIENTPLATFORM_REQUIRED_TABLES - names)
        if missing:
            return False, "clientplatform_schema_missing:" + ",".join(missing)
        return True, None
    except sqlite3.Error as exc:
        return _schema_error(exc)
    except OSError as exc:
        return _schema_error(exc)
    except RuntimeError as exc:
        return _schema_error(exc)
    except TypeError as exc:
        return _schema_error(exc)
    except ValueError as exc:
        return _schema_error(exc)
    except AttributeError as exc:
        return _schema_error(exc)


def _schema_wait_timeout_seconds() -> float:
    return env_float(
        "CLIENTPLATFORM_RUNTIME_SCHEMA_WAIT_SEC",
        60.0,
        minimum=1.0,
        maximum=600.0,
    )


def _schema_poll_interval_seconds() -> float:
    return env_float(
        "CLIENTPLATFORM_RUNTIME_SCHEMA_POLL_SEC",
        0.25,
        minimum=0.05,
        maximum=5.0,
    )


async def run_clientplatform_runtime_owner(
    *,
    config: DispatchRuntimeConfig | None = None,
    schema_probe: SchemaProbe = _clientplatform_schema_readiness,
    sleep: Sleep = asyncio.sleep,
    monotonic: Monotonic = time.monotonic,
) -> None:
    """Own the optional clientplatform dispatch runtime for the application lifetime.

    The owner is created by the canonical ``TaskManager``. It remains completely
    dormant unless clientplatform dispatch is explicitly enabled, waits for all
    additive ClientPlatform and account-identity tables, periodically reports
    schema-readiness delays, starts exactly one scheduler and guarantees a
    matching stop on cancellation during graceful shutdown or self-heal restart.
    """

    selected = config or dispatch_runtime_config()
    if not selected.enabled:
        return

    warning_interval = _schema_wait_timeout_seconds()
    deadline = monotonic() + warning_interval
    poll_interval = _schema_poll_interval_seconds()
    last_error = "clientplatform_schema_not_ready"
    while True:
        ready, error = await asyncio.to_thread(schema_probe)
        if ready:
            break
        last_error = str(error or last_error)
        now = monotonic()
        if now >= deadline:
            log.warning(
                "clientplatform dispatch runtime is still waiting for schema; "
                "continuing without dropping lifecycle ownership: %s",
                last_error,
            )
            deadline = now + warning_interval
        await sleep(poll_interval)

    runtime = build_dispatch_runtime(selected)
    started = await start_clientplatform_runtime(runtime)
    if not started:
        log.info("clientplatform dispatch runtime already owned or disabled")
        return

    log.info("clientplatform dispatch runtime started")
    try:
        await asyncio.Event().wait()
    finally:
        await stop_clientplatform_runtime()
        log.info("clientplatform dispatch runtime stopped")

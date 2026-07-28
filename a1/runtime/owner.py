from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable

from a1.runtime.dispatch_runtime import (
    DispatchRuntimeConfig,
    build_dispatch_runtime,
    dispatch_runtime_config,
)
from a1.runtime.lifecycle import start_a1_runtime, stop_a1_runtime
from core.runtime_env import env_float
from services.db import get_connection
from services.db.runtime import CONFIG

log = logging.getLogger(__name__)

_A1_REQUIRED_TABLES = frozenset(
    {
        "businesses",
        "business_members",
        "customers",
        "customer_identities",
        "programs",
        "lessons",
        "enrollments",
        "lesson_deliveries",
        "lesson_progress",
        "connections",
        "managed_bots",
        "delivery_dispatch_outbox",
    }
)

SchemaProbe = Callable[[], tuple[bool, str | None]]
Sleep = Callable[[float], Awaitable[None]]
Monotonic = Callable[[], float]


def _a1_schema_readiness() -> tuple[bool, str | None]:
    """Verify the complete additive A1 dispatch schema before starting workers."""

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

        missing = sorted(_A1_REQUIRED_TABLES - names)
        if missing:
            return False, "a1_schema_missing:" + ",".join(missing)
        return True, None
    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError, AttributeError) as exc:
        return False, f"a1_schema:{type(exc).__name__}"


def _schema_wait_timeout_seconds() -> float:
    return env_float(
        "A1_RUNTIME_SCHEMA_WAIT_SEC",
        60.0,
        minimum=1.0,
        maximum=600.0,
    )


def _schema_poll_interval_seconds() -> float:
    return env_float(
        "A1_RUNTIME_SCHEMA_POLL_SEC",
        0.25,
        minimum=0.05,
        maximum=5.0,
    )


async def run_a1_runtime_owner(
    *,
    config: DispatchRuntimeConfig | None = None,
    schema_probe: SchemaProbe = _a1_schema_readiness,
    sleep: Sleep = asyncio.sleep,
    monotonic: Monotonic = time.monotonic,
) -> None:
    """Own the optional A1 dispatch runtime for the lifetime of the application.

    The owner is created by the canonical ``TaskManager``. It remains completely
    dormant unless A1 dispatch is explicitly enabled, waits for all additive A1
    tables, starts exactly one scheduler and guarantees a matching stop on task
    cancellation during graceful shutdown or self-heal restart.
    """

    selected = config or dispatch_runtime_config()
    if not selected.enabled:
        return

    deadline = monotonic() + _schema_wait_timeout_seconds()
    last_error = "a1_schema_not_ready"
    while True:
        ready, error = await asyncio.to_thread(schema_probe)
        if ready:
            break
        last_error = str(error or last_error)
        if monotonic() >= deadline:
            raise RuntimeError(f"a1_runtime_schema_timeout:{last_error}")
        await sleep(_schema_poll_interval_seconds())

    runtime = build_dispatch_runtime(selected)
    started = await start_a1_runtime(runtime)
    if not started:
        log.info("A1 dispatch runtime already owned or disabled")
        return

    log.info("A1 dispatch runtime started")
    try:
        await asyncio.Event().wait()
    finally:
        await stop_a1_runtime()
        log.info("A1 dispatch runtime stopped")

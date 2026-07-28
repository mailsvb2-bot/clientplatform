from __future__ import annotations

import threading
from dataclasses import asdict
from typing import Any

from a1.runtime.dispatch_runtime import DispatchRuntime, build_dispatch_runtime
from a1.runtime.scheduler import A1DispatchScheduler


_dispatch_scheduler: A1DispatchScheduler | None = None
_lifecycle_lock = threading.Lock()


async def start_a1_runtime(
    runtime: DispatchRuntime | None = None,
) -> bool:
    """Start the additive A1 runtime once when explicitly composed by the app."""

    global _dispatch_scheduler
    with _lifecycle_lock:
        if _dispatch_scheduler is not None:
            snapshot = _dispatch_scheduler.health_snapshot()
            if snapshot.running:
                return False
        scheduler = A1DispatchScheduler(runtime or build_dispatch_runtime())
        started = scheduler.start()
        if not started:
            return False
        _dispatch_scheduler = scheduler
        return True


async def stop_a1_runtime() -> None:
    global _dispatch_scheduler
    with _lifecycle_lock:
        scheduler = _dispatch_scheduler
        _dispatch_scheduler = None
    if scheduler is not None:
        await scheduler.stop()


def a1_runtime_health_snapshot() -> dict[str, Any]:
    with _lifecycle_lock:
        scheduler = _dispatch_scheduler
    if scheduler is None:
        return {
            "a1_runtime_composed": False,
            "a1_dispatch_enabled": False,
            "a1_dispatch_running": False,
            "a1_dispatch_iterations": 0,
            "a1_dispatch_claimed": 0,
            "a1_dispatch_sent": 0,
            "a1_dispatch_retried": 0,
            "a1_dispatch_dead": 0,
            "a1_dispatch_errors": 0,
            "a1_dispatch_last_error": "",
            "a1_dispatch_last_tick_age_seconds": 0,
        }
    raw = asdict(scheduler.health_snapshot())
    return {
        "a1_runtime_composed": True,
        "a1_dispatch_enabled": bool(raw["enabled"]),
        "a1_dispatch_running": bool(raw["running"]),
        "a1_dispatch_iterations": int(raw["iterations"]),
        "a1_dispatch_claimed": int(raw["claimed"]),
        "a1_dispatch_sent": int(raw["sent"]),
        "a1_dispatch_retried": int(raw["retried"]),
        "a1_dispatch_dead": int(raw["dead"]),
        "a1_dispatch_errors": int(raw["errors"]),
        "a1_dispatch_last_error": str(raw["last_error"]),
        "a1_dispatch_last_tick_age_seconds": int(raw["last_tick_age_seconds"]),
    }

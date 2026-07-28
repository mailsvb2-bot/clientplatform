from __future__ import annotations

import threading
from dataclasses import asdict
from typing import Any

from clientplatform.runtime.dispatch_runtime import DispatchRuntime, build_dispatch_runtime
from clientplatform.runtime.scheduler import ClientPlatformDispatchScheduler


_dispatch_scheduler: ClientPlatformDispatchScheduler | None = None
_lifecycle_lock = threading.Lock()


async def start_clientplatform_runtime(
    runtime: DispatchRuntime | None = None,
) -> bool:
    """Start the additive clientplatform runtime once when explicitly composed by the app."""

    global _dispatch_scheduler
    with _lifecycle_lock:
        if _dispatch_scheduler is not None:
            snapshot = _dispatch_scheduler.health_snapshot()
            if snapshot.running:
                return False
        scheduler = ClientPlatformDispatchScheduler(runtime or build_dispatch_runtime())
        started = scheduler.start()
        if not started:
            return False
        _dispatch_scheduler = scheduler
        return True


async def stop_clientplatform_runtime() -> None:
    global _dispatch_scheduler
    with _lifecycle_lock:
        scheduler = _dispatch_scheduler
        _dispatch_scheduler = None
    if scheduler is not None:
        await scheduler.stop()


def _media_gateway_snapshot() -> dict[str, Any]:
    fallback: dict[str, Any] = {
        "clientplatform_media_gateway_configured": False,
        "clientplatform_media_gateway_health_available": False,
        "clientplatform_media_gateway_running": False,
        "clientplatform_media_gateway_requests": 0,
        "clientplatform_media_gateway_denied": 0,
        "clientplatform_media_gateway_not_found": 0,
        "clientplatform_media_gateway_upstream_errors": 0,
        "clientplatform_media_gateway_bytes_streamed": 0,
        "clientplatform_media_gateway_last_error": "",
    }
    try:
        from clientplatform.runtime.media_gateway import media_gateway_health_snapshot

        return {**fallback, **dict(media_gateway_health_snapshot())}
    except ImportError:
        return fallback
    except AttributeError:
        return fallback
    except OSError:
        return fallback
    except RuntimeError:
        return fallback
    except TypeError:
        return fallback
    except ValueError:
        return fallback


def clientplatform_runtime_health_snapshot() -> dict[str, Any]:
    gateway = _media_gateway_snapshot()
    gateway_configured = bool(gateway.get("clientplatform_media_gateway_configured"))
    gateway_available = bool(gateway.get("clientplatform_media_gateway_health_available"))
    gateway_running = bool(gateway.get("clientplatform_media_gateway_running"))
    runtime_health_available = bool(
        not gateway_configured or (gateway_available and gateway_running)
    )

    with _lifecycle_lock:
        scheduler = _dispatch_scheduler
    if scheduler is None:
        return {
            "clientplatform_runtime_health_available": runtime_health_available,
            "clientplatform_runtime_composed": False,
            "clientplatform_dispatch_enabled": False,
            "clientplatform_dispatch_running": False,
            "clientplatform_dispatch_iterations": 0,
            "clientplatform_dispatch_claimed": 0,
            "clientplatform_dispatch_sent": 0,
            "clientplatform_dispatch_retried": 0,
            "clientplatform_dispatch_dead": 0,
            "clientplatform_dispatch_errors": 0,
            "clientplatform_dispatch_last_error": "",
            "clientplatform_dispatch_last_tick_age_seconds": 0,
            **gateway,
        }
    raw = asdict(scheduler.health_snapshot())
    return {
        "clientplatform_runtime_health_available": runtime_health_available,
        "clientplatform_runtime_composed": True,
        "clientplatform_dispatch_enabled": bool(raw["enabled"]),
        "clientplatform_dispatch_running": bool(raw["running"]),
        "clientplatform_dispatch_iterations": int(raw["iterations"]),
        "clientplatform_dispatch_claimed": int(raw["claimed"]),
        "clientplatform_dispatch_sent": int(raw["sent"]),
        "clientplatform_dispatch_retried": int(raw["retried"]),
        "clientplatform_dispatch_dead": int(raw["dead"]),
        "clientplatform_dispatch_errors": int(raw["errors"]),
        "clientplatform_dispatch_last_error": str(raw["last_error"]),
        "clientplatform_dispatch_last_tick_age_seconds": int(raw["last_tick_age_seconds"]),
        **gateway,
    }

from __future__ import annotations

import os
from typing import Any

from core.runtime_env import env_int


_TRUE_VALUES = frozenset({'1', 'true', 'yes', 'on'})


def a1_dispatch_configured() -> bool:
    raw = str(os.getenv('A1_DISPATCH_RUNTIME_ENABLED') or '').strip().lower()
    return raw in _TRUE_VALUES


def a1_runtime_snapshot() -> dict[str, Any]:
    configured = a1_dispatch_configured()
    fallback: dict[str, Any] = {
        'a1_dispatch_configured': configured,
        'a1_runtime_health_available': False,
        'a1_runtime_composed': False,
        'a1_dispatch_enabled': False,
        'a1_dispatch_running': False,
        'a1_dispatch_iterations': 0,
        'a1_dispatch_claimed': 0,
        'a1_dispatch_sent': 0,
        'a1_dispatch_retried': 0,
        'a1_dispatch_dead': 0,
        'a1_dispatch_errors': 0,
        'a1_dispatch_last_error': '',
        'a1_dispatch_last_tick_age_seconds': 0,
    }
    try:
        from a1.runtime.lifecycle import a1_runtime_health_snapshot

        snapshot = dict(a1_runtime_health_snapshot())
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
    return {
        **fallback,
        'a1_runtime_health_available': True,
        **snapshot,
    }


def _a1_dispatch_stale(snapshot: dict[str, Any]) -> bool:
    if not bool(snapshot.get('a1_dispatch_running')):
        return False
    try:
        iterations = int(snapshot.get('a1_dispatch_iterations') or 0)
    except (TypeError, ValueError):
        iterations = 0
    if iterations <= 0:
        return False
    max_age_sec = env_int(
        'A1_DISPATCH_READY_MAX_LAST_TICK_AGE_SEC',
        180,
        minimum=0,
        maximum=86_400,
    )
    if max_age_sec <= 0:
        return False
    try:
        age_sec = int(snapshot.get('a1_dispatch_last_tick_age_seconds') or 0)
    except (TypeError, ValueError):
        age_sec = max_age_sec + 1
    return age_sec > max_age_sec


def a1_dispatch_readiness(
    snapshot: dict[str, Any],
) -> tuple[bool, list[str], dict[str, bool]]:
    configured = bool(snapshot.get('a1_dispatch_configured'))
    health_available = bool(snapshot.get('a1_runtime_health_available'))
    composed = bool(snapshot.get('a1_runtime_composed'))
    runtime_enabled = bool(snapshot.get('a1_dispatch_enabled'))
    running = bool(snapshot.get('a1_dispatch_running'))
    last_error = str(snapshot.get('a1_dispatch_last_error') or '').strip()
    try:
        error_count = int(snapshot.get('a1_dispatch_errors') or 0)
    except (TypeError, ValueError):
        error_count = 0
    recent_error = bool(error_count > 0 and last_error)
    stale = _a1_dispatch_stale(snapshot)

    errors: list[str] = []
    if configured:
        if not health_available:
            errors.append('a1_dispatch:health_unavailable')
        elif not composed:
            errors.append('a1_dispatch:not_composed')
        else:
            if not runtime_enabled:
                errors.append('a1_dispatch:not_enabled')
            if not running:
                errors.append('a1_dispatch:not_running')
            if recent_error:
                errors.append('a1_dispatch:recent_tick_error')
            if stale:
                errors.append('a1_dispatch:stale_tick')

    degraded = bool(errors)
    ready = bool(not configured or not degraded)
    return (
        ready,
        errors,
        {
            'a1_dispatch_ready': ready,
            'a1_dispatch_recent_error': recent_error,
            'a1_dispatch_stale': stale,
            'a1_dispatch_degraded': degraded,
        },
    )

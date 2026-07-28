from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from typing import Any

from clientplatform.runtime.control_bot import control_bot_enabled
from core.runtime_env import env_int


_TRUE_VALUES = frozenset({'1', 'true', 'yes', 'on'})
_FALSE_VALUES = frozenset({'0', 'false', 'no', 'off'})
OutboxProbe = Callable[..., dict[str, Any]]


def clientplatform_dispatch_configured() -> bool:
    raw = os.getenv('CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED')
    if raw is None or not str(raw).strip():
        return control_bot_enabled()
    normalized = str(raw).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise RuntimeError('clientplatform_dispatch_runtime_enabled_invalid')


def clientplatform_runtime_snapshot() -> dict[str, Any]:
    configured = clientplatform_dispatch_configured()
    fallback: dict[str, Any] = {
        'clientplatform_dispatch_configured': configured,
        'clientplatform_runtime_health_available': False,
        'clientplatform_runtime_composed': False,
        'clientplatform_dispatch_enabled': False,
        'clientplatform_dispatch_running': False,
        'clientplatform_dispatch_iterations': 0,
        'clientplatform_dispatch_claimed': 0,
        'clientplatform_dispatch_sent': 0,
        'clientplatform_dispatch_retried': 0,
        'clientplatform_dispatch_dead': 0,
        'clientplatform_dispatch_errors': 0,
        'clientplatform_dispatch_last_error': '',
        'clientplatform_dispatch_last_tick_age_seconds': 0,
    }
    runtime = fallback
    try:
        from clientplatform.runtime.lifecycle import clientplatform_runtime_health_snapshot

        snapshot = dict(clientplatform_runtime_health_snapshot())
    except ImportError:
        pass
    except AttributeError:
        pass
    except OSError:
        pass
    except RuntimeError:
        pass
    except TypeError:
        pass
    except ValueError:
        pass
    else:
        runtime = {
            **fallback,
            'clientplatform_runtime_health_available': True,
            **snapshot,
        }
    return {
        **runtime,
        **clientplatform_outbox_snapshot(configured=configured),
    }


def clientplatform_outbox_snapshot(
    *,
    configured: bool | None = None,
    probe: OutboxProbe | None = None,
) -> dict[str, Any]:
    selected = clientplatform_dispatch_configured() if configured is None else bool(configured)
    fallback: dict[str, Any] = {
        'clientplatform_dispatch_outbox_checked': False,
        'clientplatform_dispatch_outbox_available': False,
        'clientplatform_dispatch_outbox_pending': 0,
        'clientplatform_dispatch_outbox_retry': 0,
        'clientplatform_dispatch_outbox_sending': 0,
        'clientplatform_dispatch_outbox_sent': 0,
        'clientplatform_dispatch_outbox_dead': 0,
        'clientplatform_dispatch_outbox_cancelled': 0,
        'clientplatform_dispatch_outbox_due': 0,
        'clientplatform_dispatch_outbox_stale_sending': 0,
        'clientplatform_dispatch_outbox_recent_dead': 0,
        'clientplatform_dispatch_outbox_oldest_due_age_seconds': 0,
        'clientplatform_dispatch_outbox_error': '',
    }
    if not selected:
        return fallback

    if probe is None:
        try:
            from clientplatform.infrastructure.dispatch_observability import (
                load_dispatch_outbox_snapshot,
            )
        except ImportError:
            return {
                **fallback,
                'clientplatform_dispatch_outbox_checked': True,
                'clientplatform_dispatch_outbox_error': 'ImportError',
            }
        probe = load_dispatch_outbox_snapshot

    lock_ttl_seconds = env_int(
        'CLIENTPLATFORM_DISPATCH_LOCK_TTL_SEC',
        900,
        minimum=30,
        maximum=86_400,
    )
    dead_window_seconds = env_int(
        'CLIENTPLATFORM_DISPATCH_READY_DEAD_WINDOW_SEC',
        900,
        minimum=60,
        maximum=86_400,
    )
    try:
        snapshot = dict(
            probe(
                stale_lock_seconds=lock_ttl_seconds,
                dead_window_seconds=dead_window_seconds,
            )
        )
    except sqlite3.Error as exc:
        return {
            **fallback,
            'clientplatform_dispatch_outbox_checked': True,
            'clientplatform_dispatch_outbox_error': type(exc).__name__,
        }
    except OSError as exc:
        return {
            **fallback,
            'clientplatform_dispatch_outbox_checked': True,
            'clientplatform_dispatch_outbox_error': type(exc).__name__,
        }
    except RuntimeError as exc:
        return {
            **fallback,
            'clientplatform_dispatch_outbox_checked': True,
            'clientplatform_dispatch_outbox_error': type(exc).__name__,
        }
    except TypeError as exc:
        return {
            **fallback,
            'clientplatform_dispatch_outbox_checked': True,
            'clientplatform_dispatch_outbox_error': type(exc).__name__,
        }
    except ValueError as exc:
        return {
            **fallback,
            'clientplatform_dispatch_outbox_checked': True,
            'clientplatform_dispatch_outbox_error': type(exc).__name__,
        }
    return {
        **fallback,
        'clientplatform_dispatch_outbox_checked': True,
        **snapshot,
    }


def _clientplatform_dispatch_stale(snapshot: dict[str, Any]) -> bool:
    if not bool(snapshot.get('clientplatform_dispatch_running')):
        return False
    try:
        iterations = int(snapshot.get('clientplatform_dispatch_iterations') or 0)
    except (TypeError, ValueError):
        iterations = 0
    if iterations <= 0:
        return False
    max_age_sec = env_int(
        'CLIENTPLATFORM_DISPATCH_READY_MAX_LAST_TICK_AGE_SEC',
        180,
        minimum=0,
        maximum=86_400,
    )
    if max_age_sec <= 0:
        return False
    try:
        age_sec = int(snapshot.get('clientplatform_dispatch_last_tick_age_seconds') or 0)
    except (TypeError, ValueError):
        age_sec = max_age_sec + 1
    return age_sec > max_age_sec


def _metric(snapshot: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(snapshot.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def clientplatform_outbox_readiness(
    snapshot: dict[str, Any],
    *,
    configured: bool,
) -> tuple[bool, list[str], dict[str, bool]]:
    available = bool(snapshot.get('clientplatform_dispatch_outbox_available'))
    due_count = _metric(snapshot, 'clientplatform_dispatch_outbox_due')
    stale_sending_count = _metric(snapshot, 'clientplatform_dispatch_outbox_stale_sending')
    recent_dead_count = _metric(snapshot, 'clientplatform_dispatch_outbox_recent_dead')
    oldest_due_age_seconds = _metric(
        snapshot,
        'clientplatform_dispatch_outbox_oldest_due_age_seconds',
    )

    max_due = env_int(
        'CLIENTPLATFORM_DISPATCH_READY_MAX_DUE',
        1000,
        minimum=0,
        maximum=1_000_000,
    )
    max_stale_sending = env_int(
        'CLIENTPLATFORM_DISPATCH_READY_MAX_STALE_SENDING',
        0,
        minimum=0,
        maximum=100_000,
    )
    max_recent_dead = env_int(
        'CLIENTPLATFORM_DISPATCH_READY_MAX_RECENT_DEAD',
        100,
        minimum=0,
        maximum=100_000,
    )
    max_oldest_due_age = env_int(
        'CLIENTPLATFORM_DISPATCH_READY_MAX_OLDEST_DUE_AGE_SEC',
        900,
        minimum=0,
        maximum=604_800,
    )

    due_backlog = due_count > max_due
    stale_sending = stale_sending_count > max_stale_sending
    recent_dead = recent_dead_count > max_recent_dead
    oldest_due = bool(
        due_count > 0
        and max_oldest_due_age > 0
        and oldest_due_age_seconds > max_oldest_due_age
    )

    errors: list[str] = []
    if configured:
        if not available:
            errors.append('clientplatform_dispatch_outbox:unavailable')
        else:
            if due_backlog:
                errors.append('clientplatform_dispatch_outbox:due_backlog')
            if oldest_due:
                errors.append('clientplatform_dispatch_outbox:oldest_due')
            if stale_sending:
                errors.append('clientplatform_dispatch_outbox:stale_sending')
            if recent_dead:
                errors.append('clientplatform_dispatch_outbox:recent_dead')

    degraded = bool(errors)
    ready = bool(not configured or not degraded)
    return (
        ready,
        errors,
        {
            'clientplatform_dispatch_outbox_ready': ready,
            'clientplatform_dispatch_outbox_due_backlog': due_backlog,
            'clientplatform_dispatch_outbox_oldest_due': oldest_due,
            'clientplatform_dispatch_outbox_stale_leases': stale_sending,
            'clientplatform_dispatch_outbox_recent_dead_exceeded': recent_dead,
            'clientplatform_dispatch_outbox_degraded': degraded,
        },
    )


def clientplatform_dispatch_readiness(
    snapshot: dict[str, Any],
) -> tuple[bool, list[str], dict[str, bool]]:
    configured = bool(snapshot.get('clientplatform_dispatch_configured'))
    health_available = bool(snapshot.get('clientplatform_runtime_health_available'))
    composed = bool(snapshot.get('clientplatform_runtime_composed'))
    runtime_enabled = bool(snapshot.get('clientplatform_dispatch_enabled'))
    running = bool(snapshot.get('clientplatform_dispatch_running'))
    last_error = str(snapshot.get('clientplatform_dispatch_last_error') or '').strip()
    try:
        error_count = int(snapshot.get('clientplatform_dispatch_errors') or 0)
    except (TypeError, ValueError):
        error_count = 0
    recent_error = bool(error_count > 0 and last_error)
    stale = _clientplatform_dispatch_stale(snapshot)

    runtime_errors: list[str] = []
    if configured:
        if not health_available:
            runtime_errors.append('clientplatform_dispatch:health_unavailable')
        elif not composed:
            runtime_errors.append('clientplatform_dispatch:not_composed')
        else:
            if not runtime_enabled:
                runtime_errors.append('clientplatform_dispatch:not_enabled')
            if not running:
                runtime_errors.append('clientplatform_dispatch:not_running')
            if recent_error:
                runtime_errors.append('clientplatform_dispatch:recent_tick_error')
            if stale:
                runtime_errors.append('clientplatform_dispatch:stale_tick')

    runtime_degraded = bool(runtime_errors)
    runtime_ready = bool(not configured or not runtime_degraded)
    outbox_ready, outbox_errors, outbox_flags = clientplatform_outbox_readiness(
        snapshot,
        configured=configured,
    )
    errors = [*runtime_errors, *outbox_errors]
    ready = bool(runtime_ready and outbox_ready)
    return (
        ready,
        errors,
        {
            'clientplatform_dispatch_ready': ready,
            'clientplatform_dispatch_runtime_ready': runtime_ready,
            'clientplatform_dispatch_recent_error': recent_error,
            'clientplatform_dispatch_stale': stale,
            'clientplatform_dispatch_runtime_degraded': runtime_degraded,
            **outbox_flags,
        },
    )

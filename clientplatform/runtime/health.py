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
        **clientplatform_ad_runtime_snapshot(),
    }


def clientplatform_ad_runtime_snapshot() -> dict[str, Any]:
    fallback: dict[str, Any] = {
        'clientplatform_ad_runtime_health_available': False,
        'clientplatform_ad_runtime_configured': False,
        'clientplatform_ad_runtime_configuration_ok': True,
        'clientplatform_ad_runtime_configuration_error': '',
        'clientplatform_ad_runtime_running': False,
        'clientplatform_ad_runtime_iterations': 0,
        'clientplatform_ad_publication_processed': 0,
        'clientplatform_ad_spend_operations_processed': 0,
        'clientplatform_ad_spend_guard_scanned': 0,
        'clientplatform_ad_spend_guard_allowed': 0,
        'clientplatform_ad_spend_guard_stops_queued': 0,
        'clientplatform_ad_spend_guard_failed_closed': 0,
        'clientplatform_ad_runtime_errors': 0,
        'clientplatform_ad_runtime_last_error': '',
        'clientplatform_ad_runtime_last_tick_age_seconds': 0,
        'clientplatform_ad_spend_outbox_checked': False,
        'clientplatform_ad_spend_outbox_available': False,
        'clientplatform_ad_spend_outbox_queued': 0,
        'clientplatform_ad_spend_outbox_processing': 0,
        'clientplatform_ad_spend_outbox_retry': 0,
        'clientplatform_ad_spend_outbox_succeeded': 0,
        'clientplatform_ad_spend_outbox_failed': 0,
        'clientplatform_ad_spend_outbox_due': 0,
        'clientplatform_ad_spend_outbox_stale_processing': 0,
        'clientplatform_ad_spend_outbox_recent_failed': 0,
        'clientplatform_ad_spend_outbox_oldest_due_age_seconds': 0,
        'clientplatform_ad_spend_outbox_error': '',
    }
    try:
        from clientplatform.runtime.ad_publication_worker import (
            ad_publication_worker_health_snapshot,
        )

        worker_snapshot = dict(ad_publication_worker_health_snapshot())
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

    runtime = {
        **fallback,
        'clientplatform_ad_runtime_health_available': True,
        **worker_snapshot,
    }
    if not bool(runtime.get('clientplatform_ad_runtime_configured')):
        return runtime

    stale_lock_seconds = env_int(
        'CLIENTPLATFORM_AD_SPEND_LOCK_TTL_SEC',
        300,
        minimum=30,
        maximum=86_400,
    )
    dead_window_seconds = env_int(
        'CLIENTPLATFORM_AD_SPEND_READY_DEAD_WINDOW_SEC',
        900,
        minimum=60,
        maximum=86_400,
    )
    try:
        from clientplatform.infrastructure.ad_spend_observability import (
            load_ad_spend_operation_snapshot,
        )

        outbox_snapshot = dict(
            load_ad_spend_operation_snapshot(
                stale_lock_seconds=stale_lock_seconds,
                dead_window_seconds=dead_window_seconds,
            )
        )
    except ImportError as exc:
        return {
            **runtime,
            'clientplatform_ad_spend_outbox_checked': True,
            'clientplatform_ad_spend_outbox_error': type(exc).__name__,
        }
    except sqlite3.Error as exc:
        return {
            **runtime,
            'clientplatform_ad_spend_outbox_checked': True,
            'clientplatform_ad_spend_outbox_error': type(exc).__name__,
        }
    except OSError as exc:
        return {
            **runtime,
            'clientplatform_ad_spend_outbox_checked': True,
            'clientplatform_ad_spend_outbox_error': type(exc).__name__,
        }
    except RuntimeError as exc:
        return {
            **runtime,
            'clientplatform_ad_spend_outbox_checked': True,
            'clientplatform_ad_spend_outbox_error': type(exc).__name__,
        }
    except TypeError as exc:
        return {
            **runtime,
            'clientplatform_ad_spend_outbox_checked': True,
            'clientplatform_ad_spend_outbox_error': type(exc).__name__,
        }
    except ValueError as exc:
        return {
            **runtime,
            'clientplatform_ad_spend_outbox_checked': True,
            'clientplatform_ad_spend_outbox_error': type(exc).__name__,
        }
    return {
        **runtime,
        'clientplatform_ad_spend_outbox_checked': True,
        **outbox_snapshot,
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


def _clientplatform_ad_runtime_stale(snapshot: dict[str, Any]) -> bool:
    if not bool(snapshot.get('clientplatform_ad_runtime_running')):
        return False
    try:
        iterations = int(snapshot.get('clientplatform_ad_runtime_iterations') or 0)
    except (TypeError, ValueError):
        iterations = 0
    if iterations <= 0:
        return False
    max_age_sec = env_int(
        'CLIENTPLATFORM_AD_RUNTIME_READY_MAX_LAST_TICK_AGE_SEC',
        180,
        minimum=0,
        maximum=86_400,
    )
    if max_age_sec <= 0:
        return False
    try:
        age_sec = int(
            snapshot.get('clientplatform_ad_runtime_last_tick_age_seconds') or 0
        )
    except (TypeError, ValueError):
        age_sec = max_age_sec + 1
    return age_sec > max_age_sec


def _metric(snapshot: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(snapshot.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def clientplatform_ad_runtime_readiness(
    snapshot: dict[str, Any],
) -> tuple[bool, list[str], dict[str, bool]]:
    configured = bool(snapshot.get('clientplatform_ad_runtime_configured'))
    health_available = bool(
        snapshot.get('clientplatform_ad_runtime_health_available')
    )
    configuration_ok = bool(
        snapshot.get('clientplatform_ad_runtime_configuration_ok')
    )
    running = bool(snapshot.get('clientplatform_ad_runtime_running'))
    stale = _clientplatform_ad_runtime_stale(snapshot)
    recent_error = bool(
        _metric(snapshot, 'clientplatform_ad_runtime_errors') > 0
        and str(snapshot.get('clientplatform_ad_runtime_last_error') or '').strip()
    )
    outbox_available = bool(
        snapshot.get('clientplatform_ad_spend_outbox_available')
    )
    due = _metric(snapshot, 'clientplatform_ad_spend_outbox_due')
    stale_processing_count = _metric(
        snapshot,
        'clientplatform_ad_spend_outbox_stale_processing',
    )
    recent_failed_count = _metric(
        snapshot,
        'clientplatform_ad_spend_outbox_recent_failed',
    )
    oldest_due_age = _metric(
        snapshot,
        'clientplatform_ad_spend_outbox_oldest_due_age_seconds',
    )
    max_due = env_int(
        'CLIENTPLATFORM_AD_SPEND_READY_MAX_DUE',
        100,
        minimum=0,
        maximum=100_000,
    )
    max_stale_processing = env_int(
        'CLIENTPLATFORM_AD_SPEND_READY_MAX_STALE_PROCESSING',
        0,
        minimum=0,
        maximum=100_000,
    )
    max_recent_failed = env_int(
        'CLIENTPLATFORM_AD_SPEND_READY_MAX_RECENT_FAILED',
        0,
        minimum=0,
        maximum=100_000,
    )
    max_oldest_due_age = env_int(
        'CLIENTPLATFORM_AD_SPEND_READY_MAX_OLDEST_DUE_AGE_SEC',
        300,
        minimum=0,
        maximum=86_400,
    )
    due_backlog = due > max_due
    stale_processing = stale_processing_count > max_stale_processing
    recent_failed = recent_failed_count > max_recent_failed
    oldest_due = bool(
        due > 0
        and max_oldest_due_age > 0
        and oldest_due_age > max_oldest_due_age
    )

    errors: list[str] = []
    if configured:
        if not health_available:
            errors.append('clientplatform_ad_runtime:health_unavailable')
        if not configuration_ok:
            errors.append('clientplatform_ad_runtime:configuration_invalid')
        if not running:
            errors.append('clientplatform_ad_runtime:not_running')
        if recent_error:
            errors.append('clientplatform_ad_runtime:recent_tick_error')
        if stale:
            errors.append('clientplatform_ad_runtime:stale_tick')
        if not outbox_available:
            errors.append('clientplatform_ad_spend_outbox:unavailable')
        else:
            if due_backlog:
                errors.append('clientplatform_ad_spend_outbox:due_backlog')
            if oldest_due:
                errors.append('clientplatform_ad_spend_outbox:oldest_due')
            if stale_processing:
                errors.append('clientplatform_ad_spend_outbox:stale_processing')
            if recent_failed:
                errors.append('clientplatform_ad_spend_outbox:recent_failed')

    degraded = bool(errors)
    ready = bool(not configured or not degraded)
    return (
        ready,
        errors,
        {
            'clientplatform_ad_runtime_ready': ready,
            'clientplatform_ad_runtime_recent_error': recent_error,
            'clientplatform_ad_runtime_stale': stale,
            'clientplatform_ad_spend_outbox_due_backlog': due_backlog,
            'clientplatform_ad_spend_outbox_oldest_due': oldest_due,
            'clientplatform_ad_spend_outbox_stale_processing': stale_processing,
            'clientplatform_ad_spend_outbox_recent_failed_exceeded': recent_failed,
            'clientplatform_ad_runtime_degraded': degraded,
        },
    )


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
    ad_ready, ad_errors, ad_flags = clientplatform_ad_runtime_readiness(snapshot)
    errors = [*runtime_errors, *outbox_errors, *ad_errors]
    ready = bool(runtime_ready and outbox_ready and ad_ready)
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
            **ad_flags,
        },
    )

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from a1.runtime.health import a1_dispatch_readiness, a1_runtime_snapshot
from config.settings import settings
from core.paths import DB_PATH, ROOT
from runtime.ingress_flags import (
    http_ingress_enabled,
    max_webhook_enabled,
    payment_http_enabled,
    vk_webhook_enabled,
)
from runtime.telegram_transport import telegram_transport
from services.ai.policy import ai_policy_snapshot
from services.db import get_connection
from services.db.runtime import CONFIG, redacted_db_target
from services.db.schema.readiness import required_readiness_tables, schema_readiness
from services.growth_click_tracking import build_click_redirect_target, record_click_redirect
from services.messenger.preflight import check_all_preflights
from services.scheduler import scheduler_health_snapshot
from services.validators.audio import audio_readiness

log = logging.getLogger(__name__)


_DIAGNOSTICS_HEADER = 'X-Metrotherapy-Diagnostics-Token'
_DIAGNOSTICS_ENV = 'HEALTHCHECK_DIAGNOSTICS_TOKEN'


def _diagnostics_token() -> str:
    return str(os.getenv(_DIAGNOSTICS_ENV) or '').strip()


def _provided_diagnostics_token(request: web.Request) -> str:
    headers = getattr(request, 'headers', {}) or {}
    explicit = str(headers.get(_DIAGNOSTICS_HEADER) or '').strip()
    if explicit:
        return explicit
    authorization = str(headers.get('Authorization') or '').strip()
    scheme, separator, value = authorization.partition(' ')
    if separator and scheme.casefold() == 'bearer':
        return value.strip()
    return ''


def _diagnostics_authorized(request: web.Request) -> bool:
    expected = _diagnostics_token()
    provided = _provided_diagnostics_token(request)
    return bool(expected and provided and hmac.compare_digest(provided, expected))


def _public_probe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        'ok': bool(payload.get('ok')),
        'service': str(payload.get('service') or 'metrotherapy'),
        'probe': str(payload.get('probe') or 'health'),
    }


@dataclass
class HealthRuntime:
    runner: web.AppRunner
    site: web.TCPSite

    async def stop(self) -> None:
        await self.runner.cleanup()


def _scheduler_snapshot() -> dict[str, Any]:
    try:
        return scheduler_health_snapshot()
    except (ImportError, AttributeError, RuntimeError):
        return {
            'scheduler_loop_task_running': False,
            'precise_scheduler_running': False,
            'precise_scheduler_task_running': False,
            'precise_scheduler_queue_size': 0,
            'scheduler_loop_error_count': 0,
            'scheduler_loop_last_error': '',
            'scheduler_loop_last_error_age_sec': 0,
            'scheduler_loop_last_tick_age_sec': 0,
        }


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == '':
        return int(default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning('Bad integer env %s=%r; using default=%s', name, raw, default)
        return int(default)


def _scheduler_recent_error(scheduler: dict[str, Any]) -> bool:
    """Return true when the protected scheduler loop has a fresh owner-tick failure.

    The scheduler intentionally keeps running when one owner tick crashes. That is
    good for containment, but readiness must not stay green while auto-audio,
    engine jobs, reward aggregation or UX guard are repeatedly failing. Health
    remains informational; /readyz is the deployment gate.
    """
    try:
        error_count = int(scheduler.get('scheduler_loop_error_count') or 0)
    except (TypeError, ValueError):
        error_count = 0
    if error_count <= 0:
        return False
    if not str(scheduler.get('scheduler_loop_last_error') or '').strip():
        return False

    max_age_sec = _int_env('SCHEDULER_READY_MAX_LAST_ERROR_AGE_SEC', 300)
    if max_age_sec <= 0:
        return True
    try:
        age_sec = int(scheduler.get('scheduler_loop_last_error_age_sec') or 0)
    except (TypeError, ValueError):
        age_sec = 0
    return age_sec <= max_age_sec


def _scheduler_stale(scheduler: dict[str, Any]) -> bool:
    if not bool(scheduler.get('scheduler_loop_task_running')):
        return False
    if not bool(scheduler.get('scheduler_loop_started')):
        return False
    max_age_sec = _int_env('SCHEDULER_READY_MAX_LAST_TICK_AGE_SEC', 15)
    if max_age_sec <= 0:
        return False
    try:
        age_sec = int(scheduler.get('scheduler_loop_last_tick_age_sec') or 0)
    except (TypeError, ValueError):
        age_sec = max_age_sec + 1
    return age_sec > max_age_sec


def _scheduler_readiness(scheduler: dict[str, Any]) -> tuple[bool, list[str], dict[str, bool]]:
    running = bool(scheduler.get('scheduler_loop_task_running'))
    recent_error = _scheduler_recent_error(scheduler)
    stale = _scheduler_stale(scheduler)
    try:
        payment_retry_active = int(scheduler.get('payment_retry_active') or 0)
    except (TypeError, ValueError):
        payment_retry_active = -1
    try:
        payment_retry_dead_count = int(scheduler.get('payment_retry_dead') or 0)
    except (TypeError, ValueError):
        payment_retry_dead_count = -1
    max_active = _int_env('PAYMENT_RETRY_READY_MAX_ACTIVE', 1000)
    max_dead = _int_env('PAYMENT_RETRY_READY_MAX_DEAD', 0)
    payment_retry_unavailable = payment_retry_active < 0 or payment_retry_dead_count < 0
    payment_retry_backlog = payment_retry_active > max_active
    payment_retry_dead = payment_retry_dead_count > max_dead

    errors: list[str] = []
    if not running:
        errors.append('scheduler:not_running')
    if recent_error:
        errors.append('scheduler:recent_owner_tick_error')
    if stale:
        errors.append('scheduler:stale_tick')
    if payment_retry_unavailable:
        errors.append('payment_retry:unavailable')
    if payment_retry_backlog:
        errors.append('payment_retry:backlog')
    if payment_retry_dead:
        errors.append('payment_retry:dead_letter')

    degraded = bool(
        recent_error
        or stale
        or payment_retry_unavailable
        or payment_retry_backlog
        or payment_retry_dead
    )
    return (
        bool(running and not degraded),
        errors,
        {
            'scheduler_recent_error': recent_error,
            'scheduler_stale': stale,
            'payment_retry_unavailable': payment_retry_unavailable,
            'payment_retry_backlog': payment_retry_backlog,
            'payment_retry_dead_letter': payment_retry_dead,
            'scheduler_degraded': degraded,
        },
    )


def _messenger_webhook_configured() -> bool:
    """Legacy diagnostic field retained for operator/dashboard compatibility."""
    try:
        return bool(getattr(settings, 'MESSENGER_WEBHOOK_ENABLED', False) or False)
    except (AttributeError, RuntimeError):
        return False


def _telegram_transport() -> str:
    try:
        return telegram_transport()
    except (AttributeError, RuntimeError):
        return 'unknown'


def _telegram_webhook_configured() -> bool:
    return _telegram_transport() == 'webhook'


def _webhook_configured() -> bool:
    return bool(http_ingress_enabled() or _telegram_webhook_configured())


def _db_ready() -> tuple[bool, str | None]:
    try:
        with get_connection() as conn:
            conn.execute('SELECT 1').fetchone()
        return True, None
    except Exception as exc:  # validator: allow-wide-except
        return False, f'db:{exc}'


def _schema_ready() -> tuple[bool, str | None]:
    return schema_readiness()


def _audio_ready(app_env: str) -> tuple[bool, str | None]:
    if app_env not in {'prod', 'production'}:
        return True, None
    return audio_readiness()


def _storage_health_fields() -> dict[str, Any]:
    fields: dict[str, Any] = {'root_exists': False}
    try:
        fields['root_exists'] = ROOT.exists()
        if CONFIG.uses_postgres:
            fields['legacy_sqlite_path'] = str(DB_PATH)
            fields['legacy_sqlite_present'] = Path(DB_PATH).exists()
        else:
            fields['db_path'] = str(DB_PATH)
            fields['db_exists'] = Path(DB_PATH).exists()
    except OSError:
        fields['root_exists'] = False
        if CONFIG.uses_postgres:
            fields['legacy_sqlite_path'] = str(DB_PATH)
            fields['legacy_sqlite_present'] = False
        else:
            fields['db_path'] = str(DB_PATH)
            fields['db_exists'] = False
    return fields


def _messenger_preflight_readiness() -> tuple[bool, list[str], dict[str, Any]]:
    """Validate only enabled ingress channels.

    The function name is kept for compatibility with existing diagnostics/tests;
    the contract now covers payment, MAX and VK independently.
    """
    statuses = check_all_preflights()
    details: dict[str, Any] = {}
    errors: list[str] = []
    for status in statuses:
        status_details = dict(status.details or {})
        enabled = bool(status_details.get('enabled', True))
        details[f'{status.channel}_preflight_enabled'] = enabled
        details[f'{status.channel}_preflight_ok'] = bool(status.ok)
        details[f'{status.channel}_preflight_missing'] = list(status.missing)
        details[f'{status.channel}_preflight_warnings'] = list(status.warnings)
        if status.details:
            details[f'{status.channel}_preflight_details'] = status.details
        if enabled and not status.ok:
            errors.append(f"ingress:{status.channel}:missing:{','.join(status.missing)}")
    return not errors, errors, details


def _ingress_health_fields() -> dict[str, bool]:
    return {
        'payment_http_enabled': payment_http_enabled(),
        'max_webhook_enabled': max_webhook_enabled(),
        'vk_webhook_enabled': vk_webhook_enabled(),
        'http_ingress_enabled': http_ingress_enabled(),
    }


def build_health_payload() -> tuple[dict[str, Any], int]:
    scheduler = _scheduler_snapshot()
    a1_runtime = a1_runtime_snapshot()
    telegram_transport_value = _telegram_transport()
    messenger_webhook_enabled = _messenger_webhook_configured()
    telegram_webhook_enabled = telegram_transport_value == 'webhook'
    webhook_runtime_enabled = _webhook_configured()
    _, _, messenger_preflight_fields = _messenger_preflight_readiness()
    details: dict[str, Any] = {
        'ok': True,
        'service': 'metrotherapy',
        'probe': 'health',
        'db_engine': CONFIG.engine,
        'db_target': redacted_db_target(),
        'telegram_transport': telegram_transport_value,
        'telegram_webhook_enabled': telegram_webhook_enabled,
        'messenger_webhook_enabled': messenger_webhook_enabled,
        'webhook_runtime_enabled': webhook_runtime_enabled,
        'app_env': (os.getenv('APP_ENV', 'dev') or 'dev').strip().lower(),
        **_ingress_health_fields(),
        **_storage_health_fields(),
        **messenger_preflight_fields,
        **ai_policy_snapshot(),
        **scheduler,
        **a1_runtime,
    }
    return details, 200


def build_readiness_payload() -> tuple[dict[str, Any], int]:
    db_ok, db_error = _db_ready()
    schema_ok, schema_error = _schema_ready()
    scheduler = _scheduler_snapshot()
    a1_runtime = a1_runtime_snapshot()
    telegram_transport_value = _telegram_transport()
    messenger_webhook_enabled = _messenger_webhook_configured()
    telegram_webhook_enabled = telegram_transport_value == 'webhook'
    webhook_runtime_enabled = _webhook_configured()
    app_env = (os.getenv('APP_ENV', 'dev') or 'dev').strip().lower()
    scheduler_ok, scheduler_errors, scheduler_flags = _scheduler_readiness(scheduler)
    a1_ok, a1_errors, a1_flags = a1_dispatch_readiness(a1_runtime)
    ingress_ok, ingress_errors, ingress_fields = _messenger_preflight_readiness()
    audio_ok, audio_error = _audio_ready(app_env)
    webhook_ok = True
    if app_env in {'prod', 'production'} and (http_ingress_enabled() or telegram_webhook_enabled):
        webhook_ok = webhook_runtime_enabled
    errors: list[str] = []
    if db_error is not None:
        errors.append(db_error)
    if schema_error is not None:
        errors.append(schema_error)
    if audio_error is not None:
        errors.append(audio_error)
    errors.extend(scheduler_errors)
    errors.extend(a1_errors)
    errors.extend(ingress_errors)
    if not webhook_ok:
        errors.append('webhook:not_ready')
    ready = bool(
        db_ok
        and schema_ok
        and scheduler_ok
        and a1_ok
        and ingress_ok
        and audio_ok
        and webhook_ok
    )
    details: dict[str, Any] = {
        'ok': ready,
        'service': 'metrotherapy',
        'probe': 'readiness',
        'db_ready': db_ok,
        'schema_ready': schema_ok,
        'audio_ready': audio_ok,
        'scheduler_ready': scheduler_ok,
        'a1_dispatch_ready': a1_ok,
        'messenger_ready': ingress_ok,
        'ingress_ready': ingress_ok,
        'webhook_ready': webhook_ok,
        'required_tables': required_readiness_tables(),
        'db_engine': CONFIG.engine,
        'db_target': redacted_db_target(),
        'telegram_transport': telegram_transport_value,
        'telegram_webhook_enabled': telegram_webhook_enabled,
        'messenger_webhook_enabled': messenger_webhook_enabled,
        'webhook_runtime_enabled': webhook_runtime_enabled,
        'app_env': app_env,
        **_ingress_health_fields(),
        **scheduler_flags,
        **a1_flags,
        **_storage_health_fields(),
        **ingress_fields,
        **ai_policy_snapshot(),
        **scheduler,
        **a1_runtime,
    }
    if errors:
        details['error'] = ';'.join(errors)
        return details, 500
    return details, 200


async def _health(request: web.Request) -> web.Response:
    payload, status = await asyncio.to_thread(build_health_payload)
    response_payload = payload if _diagnostics_authorized(request) else _public_probe_payload(payload)
    return web.json_response(response_payload, status=status)


async def _ready(request: web.Request) -> web.Response:
    payload, status = await asyncio.to_thread(build_readiness_payload)
    response_payload = payload if _diagnostics_authorized(request) else _public_probe_payload(payload)
    return web.json_response(response_payload, status=status)


async def _growth_click_redirect(request: web.Request) -> web.Response:
    payload = str(request.match_info.get('payload') or '')
    target = build_click_redirect_target(payload)
    request_meta = {
        'user_agent': request.headers.get('User-Agent', ''),
        'referer': request.headers.get('Referer', ''),
    }
    try:
        await asyncio.to_thread(record_click_redirect, payload, request_meta=request_meta)
    except RuntimeError:
        log.debug('growth click tracking skipped', exc_info=True)
    except OSError:
        log.debug('growth click tracking skipped', exc_info=True)
    except TypeError:
        log.debug('growth click tracking skipped', exc_info=True)
    except ValueError:
        log.debug('growth click tracking skipped', exc_info=True)
    return web.HTTPFound(target)


async def start_health_runtime() -> HealthRuntime | None:
    enabled = (getattr(settings, 'HEALTHCHECK_ENABLED', True) or False)
    if not enabled:
        return None
    host = getattr(settings, 'HEALTHCHECK_HOST', '127.0.0.1')
    port = int(getattr(settings, 'HEALTHCHECK_PORT', 8082))
    app = web.Application()
    app.router.add_get('/a/{payload}', _growth_click_redirect)
    app.router.add_get('/health', _health)
    app.router.add_get('/healthz', _health)
    app.router.add_get('/readyz', _ready)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host=host, port=port)
        await site.start()
        log.info('Health runtime started on %s:%s', host, port)
        return HealthRuntime(runner=runner, site=site)
    except Exception:  # validator: allow-wide-except
        await runner.cleanup()
        raise

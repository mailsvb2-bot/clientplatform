from __future__ import annotations

import asyncio
import hmac
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from clientplatform.runtime.health import clientplatform_dispatch_readiness, clientplatform_runtime_snapshot
from config.settings import settings
from core.paths import DB_PATH, ROOT
from runtime.ingress_flags import (
    http_ingress_enabled,
    max_webhook_enabled,
    vk_webhook_enabled,
)
from runtime.telegram_transport import telegram_transport
from services.ai.policy import ai_policy_snapshot
from services.db import get_connection
from services.db.runtime import CONFIG, redacted_db_target
from services.db.schema.readiness import required_readiness_tables, schema_readiness
from services.messenger.start_redirect_compat import historical_start_redirect
from services.messenger.preflight import check_all_preflights

log = logging.getLogger(__name__)


_SERVICE_NAME = 'clientplatform'
_DIAGNOSTICS_HEADER = 'X-ClientPlatform-Diagnostics-Token'
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
        'service': str(payload.get('service') or _SERVICE_NAME),
        'probe': str(payload.get('probe') or 'health'),
    }


@dataclass
class HealthRuntime:
    runner: web.AppRunner
    site: web.TCPSite

    async def stop(self) -> None:
        await self.runner.cleanup()


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
        'max_webhook_enabled': max_webhook_enabled(),
        'vk_webhook_enabled': vk_webhook_enabled(),
        'http_ingress_enabled': http_ingress_enabled(),
    }


def build_health_payload() -> tuple[dict[str, Any], int]:
    clientplatform_runtime = clientplatform_runtime_snapshot()
    telegram_transport_value = _telegram_transport()
    messenger_webhook_enabled = _messenger_webhook_configured()
    telegram_webhook_enabled = telegram_transport_value == 'webhook'
    webhook_runtime_enabled = _webhook_configured()
    _, _, messenger_preflight_fields = _messenger_preflight_readiness()
    details: dict[str, Any] = {
        'ok': True,
        'service': _SERVICE_NAME,
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
        **clientplatform_runtime,
    }
    return details, 200


def build_readiness_payload() -> tuple[dict[str, Any], int]:
    db_ok, db_error = _db_ready()
    schema_ok, schema_error = _schema_ready()
    clientplatform_runtime = clientplatform_runtime_snapshot()
    telegram_transport_value = _telegram_transport()
    messenger_webhook_enabled = _messenger_webhook_configured()
    telegram_webhook_enabled = telegram_transport_value == 'webhook'
    webhook_runtime_enabled = _webhook_configured()
    app_env = (os.getenv('APP_ENV', 'dev') or 'dev').strip().lower()
    clientplatform_ok, clientplatform_errors, clientplatform_flags = clientplatform_dispatch_readiness(clientplatform_runtime)
    ingress_ok, ingress_errors, ingress_fields = _messenger_preflight_readiness()
    webhook_ok = True
    if app_env in {'prod', 'production'} and (http_ingress_enabled() or telegram_webhook_enabled):
        webhook_ok = webhook_runtime_enabled
    errors: list[str] = []
    if db_error is not None:
        errors.append(db_error)
    if schema_error is not None:
        errors.append(schema_error)
    errors.extend(clientplatform_errors)
    errors.extend(ingress_errors)
    if not webhook_ok:
        errors.append('webhook:not_ready')
    ready = bool(
        db_ok
        and schema_ok
        and clientplatform_ok
        and ingress_ok
        and webhook_ok
    )
    details: dict[str, Any] = {
        'ok': ready,
        'service': _SERVICE_NAME,
        'probe': 'readiness',
        'db_ready': db_ok,
        'schema_ready': schema_ok,
        'clientplatform_dispatch_ready': clientplatform_ok,
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
        **clientplatform_flags,
        **_storage_health_fields(),
        **ingress_fields,
        **ai_policy_snapshot(),
        **clientplatform_runtime,
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


async def _historical_start_redirect(request: web.Request) -> web.Response:
    # Compatibility-only route for links that may already exist outside the
    # repository.  It performs no attribution write and owns no campaign state.
    payload = str(request.match_info.get("payload") or "")
    return web.HTTPFound(historical_start_redirect(payload))


async def start_health_runtime() -> HealthRuntime | None:
    enabled = (getattr(settings, 'HEALTHCHECK_ENABLED', True) or False)
    if not enabled:
        return None
    host = getattr(settings, 'HEALTHCHECK_HOST', '127.0.0.1')
    port = int(getattr(settings, 'HEALTHCHECK_PORT', 8082))
    app = web.Application()
    app.router.add_get('/a/{payload}', _historical_start_redirect)
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

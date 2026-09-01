from __future__ import annotations

import pytest

from runtime import health_server


@pytest.mark.asyncio
async def test_health_handler_reports_ok(tmp_path, monkeypatch):
    class DummyCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return list(self._rows)

    class DummyConn:
        def execute(self, query: str, *args, **kwargs):
            if query == 'SELECT 1':
                return DummyCursor([(1,)])
            if 'sqlite_master' in query:
                return DummyCursor([('users',), ('jobs',)])
            raise AssertionError(query)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.delenv('HEALTHCHECK_DIAGNOSTICS_TOKEN', raising=False)
    monkeypatch.setattr(health_server, 'get_connection', lambda: DummyConn())
    monkeypatch.setattr(health_server, 'DB_PATH', tmp_path / 'data.db')
    monkeypatch.setattr(health_server, 'ROOT', tmp_path)
    monkeypatch.setattr(health_server, '_messenger_webhook_configured', lambda: True)
    monkeypatch.setattr(health_server, '_telegram_transport', lambda: 'polling')

    response = await health_server._health(None)  # type: ignore[arg-type]
    assert response.status == 200
    assert response.text
    assert '"service": "clientplatform"' in response.text
    assert '"probe": "health"' in response.text
    assert 'precise_scheduler_queue_size' not in response.text
    assert 'telegram_transport' not in response.text
    assert 'telegram_webhook_enabled' not in response.text
    assert 'messenger_webhook_enabled' not in response.text
    assert 'webhook_runtime_enabled' not in response.text


@pytest.mark.asyncio
async def test_health_handler_reports_db_failure(tmp_path, monkeypatch):
    def _boom():
        raise RuntimeError('broken')

    monkeypatch.delenv('HEALTHCHECK_DIAGNOSTICS_TOKEN', raising=False)
    monkeypatch.setattr(health_server, 'get_connection', _boom)
    monkeypatch.setattr(health_server, 'DB_PATH', tmp_path / 'data.db')
    monkeypatch.setattr(health_server, 'ROOT', tmp_path)

    response = await health_server._ready(None)  # type: ignore[arg-type]
    assert response.status == 500
    assert '"ok": false' in response.text
    assert '"service": "clientplatform"' in response.text
    assert '"probe": "readiness"' in response.text
    assert 'db:broken' not in response.text


def test_build_health_payload_reports_schema_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(health_server, 'DB_PATH', tmp_path / 'data.db')
    monkeypatch.setattr(health_server, 'ROOT', tmp_path)
    monkeypatch.setattr(health_server, '_schema_ready', lambda: (False, 'schema_missing:jobs'))

    payload, status = health_server.build_readiness_payload()
    assert status == 500
    assert payload['service'] == 'clientplatform'
    assert payload['db_ready'] is True
    assert payload['schema_ready'] is False
    assert payload['error'].startswith('schema_missing:')
    assert 'jobs' in payload['error']


def test_build_health_payload_reports_hybrid_polling_plus_http_ingress(monkeypatch, tmp_path):
    class DummyCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return list(self._rows)

    class DummyConn:
        def execute(self, query: str, *args, **kwargs):
            if query == 'SELECT 1':
                return DummyCursor([(1,)])
            if 'sqlite_master' in query:
                return DummyCursor([('users',), ('jobs',)])
            raise AssertionError(query)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(health_server, 'get_connection', lambda: DummyConn())
    monkeypatch.setattr(health_server, 'DB_PATH', tmp_path / 'data.db')
    monkeypatch.setattr(health_server, 'ROOT', tmp_path)
    monkeypatch.setattr(health_server, '_messenger_webhook_configured', lambda: True)
    monkeypatch.setattr(health_server, 'http_ingress_enabled', lambda: True)
    monkeypatch.setattr(health_server, '_telegram_transport', lambda: 'polling')

    payload, status = health_server.build_health_payload()

    assert status == 200
    assert payload['service'] == 'clientplatform'
    assert payload['telegram_transport'] == 'polling'
    assert payload['telegram_webhook_enabled'] is False
    assert payload['messenger_webhook_enabled'] is True
    assert payload['webhook_runtime_enabled'] is True

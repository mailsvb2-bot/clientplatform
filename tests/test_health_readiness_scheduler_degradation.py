from __future__ import annotations

from runtime import health_server


def _healthy_base(monkeypatch, scheduler: dict):
    monkeypatch.setattr(health_server, '_db_ready', lambda: (True, None))
    monkeypatch.setattr(health_server, '_schema_ready', lambda: (True, None))
    monkeypatch.setattr(
        health_server,
        'clientplatform_dispatch_readiness',
        lambda _snapshot: (True, [], {'clientplatform_dispatch_degraded': False}),
    )
    monkeypatch.setattr(health_server, '_scheduler_snapshot', lambda: scheduler)
    monkeypatch.setattr(health_server, '_messenger_webhook_configured', lambda: False)
    monkeypatch.setattr(health_server, '_telegram_transport', lambda: 'polling')


def test_readiness_fails_on_recent_scheduler_owner_tick_error(monkeypatch):
    _healthy_base(
        monkeypatch,
        {
            'scheduler_loop_task_running': True,
            'scheduler_loop_started': True,
            'scheduler_loop_error_count': 1,
            'scheduler_loop_last_error': 'engine.tick:RuntimeError:boom',
            'scheduler_loop_last_error_age_sec': 3,
            'scheduler_loop_last_tick_age_sec': 1,
        },
    )

    payload, status = health_server.build_readiness_payload()

    assert status == 500
    assert payload['ok'] is False
    assert payload['scheduler_ready'] is False
    assert payload['scheduler_degraded'] is True
    assert payload['scheduler_recent_error'] is True
    assert 'scheduler:recent_owner_tick_error' in payload['error']


def test_readiness_fails_on_stale_scheduler_tick(monkeypatch):
    _healthy_base(
        monkeypatch,
        {
            'scheduler_loop_task_running': True,
            'scheduler_loop_started': True,
            'scheduler_loop_error_count': 0,
            'scheduler_loop_last_error': '',
            'scheduler_loop_last_error_age_sec': 0,
            'scheduler_loop_last_tick_age_sec': 999,
        },
    )

    payload, status = health_server.build_readiness_payload()

    assert status == 500
    assert payload['ok'] is False
    assert payload['scheduler_ready'] is False
    assert payload['scheduler_degraded'] is True
    assert payload['scheduler_stale'] is True
    assert 'scheduler:stale_tick' in payload['error']


def test_readiness_allows_old_scheduler_error_after_grace_window(monkeypatch):
    monkeypatch.setenv('SCHEDULER_READY_MAX_LAST_ERROR_AGE_SEC', '300')
    _healthy_base(
        monkeypatch,
        {
            'scheduler_loop_task_running': True,
            'scheduler_loop_started': True,
            'scheduler_loop_error_count': 1,
            'scheduler_loop_last_error': 'UXGuard.tick:RuntimeError:old',
            'scheduler_loop_last_error_age_sec': 301,
            'scheduler_loop_last_tick_age_sec': 1,
        },
    )

    payload, status = health_server.build_readiness_payload()

    assert status == 200
    assert payload['ok'] is True
    assert payload['scheduler_ready'] is True
    assert payload['scheduler_degraded'] is False

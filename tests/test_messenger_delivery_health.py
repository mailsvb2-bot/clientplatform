from services.messenger import preflight


def _snapshot(**overrides):
    base = {
        "worker_expected": False,
        "worker_active": False,
        "worker_running": True,
        "pending": 0,
        "retry": 0,
        "sending": 0,
        "sent": 0,
        "rejected": 0,
        "dead": 0,
        "oldest_pending_age_sec": 0,
        "oldest_retry_age_sec": 0,
        "oldest_sending_age_sec": 0,
    }
    base.update(overrides)
    return base


def test_delivery_preflight_is_green_before_runtime_start(monkeypatch):
    monkeypatch.setattr(preflight, "vk_webhook_enabled", lambda: True)
    monkeypatch.setattr(preflight, "max_webhook_enabled", lambda: False)
    monkeypatch.setattr(preflight, "delivery_health_snapshot", lambda: _snapshot())

    status = preflight.check_delivery_outbox_preflight()
    assert status.ok is True
    assert status.missing == ()


def test_delivery_preflight_fails_when_expected_worker_stops(monkeypatch):
    monkeypatch.setattr(preflight, "vk_webhook_enabled", lambda: True)
    monkeypatch.setattr(preflight, "max_webhook_enabled", lambda: False)
    monkeypatch.setattr(
        preflight,
        "delivery_health_snapshot",
        lambda: _snapshot(worker_expected=True, worker_running=False, pending=2, sent=10),
    )

    status = preflight.check_delivery_outbox_preflight()
    assert status.ok is False
    assert "delivery_worker(running)" in status.missing


def test_delivery_preflight_fails_closed_on_dead_letters(monkeypatch):
    monkeypatch.setattr(preflight, "vk_webhook_enabled", lambda: False)
    monkeypatch.setattr(preflight, "max_webhook_enabled", lambda: True)
    monkeypatch.setattr(
        preflight,
        "delivery_health_snapshot",
        lambda: _snapshot(
            worker_expected=True,
            worker_active=True,
            worker_running=True,
            retry=3,
            sent=10,
            dead=1,
        ),
    )

    status = preflight.check_delivery_outbox_preflight()
    assert status.ok is False
    assert "dead_letters=1" in status.missing
    assert "delivery retries pending: 3" in status.warnings


def test_delivery_preflight_fails_on_stale_pending_queue(monkeypatch):
    monkeypatch.setattr(preflight, "vk_webhook_enabled", lambda: True)
    monkeypatch.setattr(preflight, "max_webhook_enabled", lambda: False)
    monkeypatch.setenv("MESSENGER_OUTBOX_READY_MAX_PENDING_AGE_SEC", "60")
    monkeypatch.setattr(
        preflight,
        "delivery_health_snapshot",
        lambda: _snapshot(
            worker_expected=True,
            worker_active=True,
            worker_running=True,
            pending=4,
            oldest_pending_age_sec=301,
        ),
    )

    status = preflight.check_delivery_outbox_preflight()

    assert status.ok is False
    assert "oldest_pending_age_sec=301" in status.missing


def test_delivery_preflight_only_warns_about_lag_before_worker_start(monkeypatch):
    monkeypatch.setattr(preflight, "vk_webhook_enabled", lambda: True)
    monkeypatch.setattr(preflight, "max_webhook_enabled", lambda: False)
    monkeypatch.setenv("MESSENGER_OUTBOX_READY_MAX_PENDING_AGE_SEC", "60")
    monkeypatch.setattr(
        preflight,
        "delivery_health_snapshot",
        lambda: _snapshot(pending=4, oldest_pending_age_sec=301),
    )

    status = preflight.check_delivery_outbox_preflight()

    assert status.ok is True
    assert status.missing == ()
    assert "prestart delivery lag: oldest_pending_age_sec=301" in status.warnings

def test_delivery_preflight_warns_but_stays_ready_on_permanent_rejections(monkeypatch):
    monkeypatch.setattr(preflight, "vk_webhook_enabled", lambda: False)
    monkeypatch.setattr(preflight, "max_webhook_enabled", lambda: True)
    monkeypatch.setattr(
        preflight,
        "delivery_health_snapshot",
        lambda: _snapshot(
            worker_expected=True,
            worker_active=True,
            worker_running=True,
            rejected=10,
        ),
    )

    status = preflight.check_delivery_outbox_preflight()

    assert status.ok is True
    assert status.missing == ()
    assert "permanent provider rejections retained: 10" in status.warnings

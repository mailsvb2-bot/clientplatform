from __future__ import annotations

import asyncio
import sqlite3

import pytest

from clientplatform.runtime import platform_resource_monitor as monitor
from services import platform_resource_limits as limits
from services import visual_provider_health as provider_health
from services import yandex_billing_health as billing_health




@pytest.fixture(autouse=True)
def _stable_visual_provider_health(monkeypatch):
    monkeypatch.setattr(
        monitor,
        "get_visual_provider_health_snapshot",
        lambda: provider_health.VisualProviderHealthSnapshot(
            available=True,
            configured_image=("yandexart",),
            configured_video=("yandexart_motion",),
        ),
    )
    monkeypatch.setattr(
        monitor,
        "get_yandex_billing_snapshot",
        lambda: billing_health.YandexBillingSnapshot(
            configured=False,
            available=False,
        ),
    )


def _snapshot(*, used: int = 0, limit: int = 30) -> limits.PlatformResourceSnapshot:
    counter = limits.ResourceCounter(
        used=used,
        limit=limit,
        remaining=max(0, limit - used),
    )
    return limits.PlatformResourceSnapshot(
        configured=True,
        telemetry_available=True,
        base_url="http://visual-creative-gateway:8097",
        token_configured=True,
        day_utc="2026-08-11",
        resets_at="2026-08-12T00:00:00Z",
        usage_semantics="gateway_reservations_not_provider_billing",
        jobs=counter,
        image=counter,
        video=limits.ResourceCounter(used=0, limit=5, remaining=5),
        active=limits.ResourceCounter(used=1, limit=3, remaining=2),
    )


def _replace_saved(saved: dict[str, object], value: dict[str, object]) -> None:
    saved.clear()
    saved.update(value)


def test_platform_resource_snapshot_uses_gateway_usage_without_secrets(monkeypatch):
    monkeypatch.setattr(
        limits.visual_gateway,
        "gateway_snapshot",
        lambda: {
            "configured": True,
            "base_url": "http://visual-creative-gateway:8097",
            "token_configured": True,
        },
    )
    monkeypatch.setattr(
        limits.visual_gateway,
        "_json",
        lambda *_args, **_kwargs: {
            "client_id": "clientplatform",
            "usage_semantics": "gateway_reservations_not_provider_billing",
            "day_utc": "2026-08-11",
            "resets_at": "2026-08-12T00:00:00Z",
            "jobs": {"used": 21, "limit": 30, "remaining": 9},
            "image": {"used": 21, "limit": 30, "remaining": 9},
            "video": {"used": 0, "limit": 5, "remaining": 5},
            "active": {"used": 1, "limit": 3, "remaining": 2},
        },
    )

    snapshot = limits.get_platform_resource_snapshot()

    assert snapshot.telemetry_available is True
    assert snapshot.image == limits.ResourceCounter(used=21, limit=30, remaining=9)
    assert snapshot.image.percent == 70
    rendered = limits.render_platform_resource_status(snapshot)
    assert "21/30 (70%)" in rendered
    assert "официальный биллинг Yandex Cloud" in rendered
    assert "secret" not in rendered.lower()


def test_platform_resource_snapshot_fails_closed_when_usage_endpoint_is_missing(monkeypatch):
    monkeypatch.setattr(
        limits.visual_gateway,
        "gateway_snapshot",
        lambda: {
            "configured": True,
            "base_url": "http://visual-creative-gateway:8097",
            "token_configured": True,
        },
    )

    def missing(*_args, **_kwargs):
        raise limits.VisualCreativeGatewayError("visual_gateway_http_404")

    monkeypatch.setattr(limits.visual_gateway, "_json", missing)
    snapshot = limits.get_platform_resource_snapshot()

    assert snapshot.configured is True
    assert snapshot.telemetry_available is False
    assert snapshot.error_code == "visual_gateway_http_404"
    assert "endpoint /v1/usage" in limits.next_action(snapshot)


def test_warning_thresholds_are_monotonic_and_actionable():
    assert limits.warning_level(limits.ResourceCounter(20, 30, 10)) == 0
    assert limits.warning_level(limits.ResourceCounter(21, 30, 9)) == 70
    assert limits.warning_level(limits.ResourceCounter(26, 30, 4)) == 85
    assert limits.warning_level(limits.ResourceCounter(29, 30, 1)) == 95
    assert limits.warning_level(limits.ResourceCounter(30, 30, 0)) == 100

    snapshot = _snapshot(used=29)
    crossed = limits.crossed_thresholds(snapshot, {"jobs": 85, "image": 85})
    assert crossed["jobs"] == 95
    assert crossed["image"] == 95
    message = limits.render_threshold_notification(snapshot, crossed)
    assert "Что делать:" in message
    assert "Yandex Cloud" in message
    assert "увеличьте дневной лимит" in message


def test_resource_monitor_notifies_once_per_crossed_level(monkeypatch):
    snapshot = _snapshot(used=26)
    saved: dict[str, object] = {}
    sent: list[str] = []

    monkeypatch.setattr(monitor, "get_platform_resource_snapshot", lambda: snapshot)
    monkeypatch.setattr(monitor, "_load_state", lambda: dict(saved))
    monkeypatch.setattr(monitor, "_save_state", lambda value: _replace_saved(saved, value))

    async def fake_send(_bot, text: str, **_kwargs):
        sent.append(text)
        return {123}, set()

    monkeypatch.setattr(monitor, "_send_resource_operators", fake_send)
    monkeypatch.setattr(monitor, "_resource_alert_chat_ids", lambda: (123,))

    asyncio.run(monitor._tick(object()))
    assert len(sent) == 1
    assert "порог 85%" in sent[0]
    assert saved["day_utc"] == "2026-08-11"

    asyncio.run(monitor._tick(object()))
    assert len(sent) == 1


def test_resource_monitor_realerts_after_limit_is_raised_and_consumed_again(monkeypatch):
    first = _snapshot(used=29, limit=30)
    raised = _snapshot(used=29, limit=60)
    later = _snapshot(used=52, limit=60)
    saved: dict[str, object] = {}
    sent: list[str] = []
    snapshots = iter([first, raised, later])

    monkeypatch.setattr(monitor, "get_platform_resource_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(monitor, "_load_state", lambda: dict(saved))
    monkeypatch.setattr(monitor, "_save_state", lambda value: _replace_saved(saved, value))

    async def fake_send(_bot, text: str, **_kwargs):
        sent.append(text)
        return {123}, set()

    monkeypatch.setattr(monitor, "_send_resource_operators", fake_send)
    monkeypatch.setattr(monitor, "_resource_alert_chat_ids", lambda: (123,))

    asyncio.run(monitor._tick(object()))
    assert len(sent) == 1
    assert "порог 95%" in sent[-1]

    asyncio.run(monitor._tick(object()))
    assert len(sent) == 1
    assert saved["levels"]["image"] == 0

    asyncio.run(monitor._tick(object()))
    assert len(sent) == 2
    assert "порог 85%" in sent[-1]


def test_threshold_alert_retries_only_failed_operator_chat(monkeypatch):
    snapshot = _snapshot(used=26)
    saved: dict[str, object] = {}
    calls: list[int] = []

    class Bot:
        failed_once = False

        async def send_message(self, admin_id: int, _text: str) -> None:
            calls.append(admin_id)
            if admin_id == 202 and not self.failed_once:
                self.failed_once = True
                raise asyncio.TimeoutError

    monkeypatch.setattr(monitor, "get_platform_resource_snapshot", lambda: snapshot)
    monkeypatch.setattr(monitor, "_load_state", lambda: dict(saved))
    monkeypatch.setattr(monitor, "_save_state", lambda value: _replace_saved(saved, value))
    monkeypatch.setattr(monitor, "_resource_alert_chat_ids", lambda: (101, 202))

    bot = Bot()
    asyncio.run(monitor._tick(bot))
    assert calls == [101, 202]
    assert saved["threshold_pending"]["pending_chat_ids"] == [202]
    assert "pending_admin_ids" not in saved["threshold_pending"]
    assert saved.get("levels", {}) == {}

    asyncio.run(monitor._tick(bot))
    assert calls == [101, 202, 202]
    assert "threshold_pending" not in saved
    assert saved["levels"]["image"] == 85


def test_telemetry_alert_retries_only_failed_operator_chat(monkeypatch):
    snapshot = limits.PlatformResourceSnapshot(
        configured=True,
        telemetry_available=False,
        base_url="http://visual-creative-gateway:8097",
        token_configured=True,
        error_code="visual_gateway_http_404",
    )
    saved: dict[str, object] = {}
    calls: list[int] = []
    operator_chat = -100777

    class Bot:
        failed_once = False

        async def send_message(self, chat_id: int, _text: str) -> None:
            calls.append(chat_id)
            if not self.failed_once:
                self.failed_once = True
                raise asyncio.TimeoutError

    monkeypatch.setattr(monitor, "get_platform_resource_snapshot", lambda: snapshot)
    monkeypatch.setattr(monitor, "_load_state", lambda: dict(saved))
    monkeypatch.setattr(monitor, "_save_state", lambda value: _replace_saved(saved, value))
    monkeypatch.setattr(monitor, "_resource_alert_chat_ids", lambda: (operator_chat,))

    bot = Bot()
    asyncio.run(monitor._tick(bot))
    assert calls == [operator_chat]
    assert saved["telemetry_pending"]["pending_chat_ids"] == [operator_chat]
    assert "pending_admin_ids" not in saved["telemetry_pending"]

    asyncio.run(monitor._tick(bot))
    assert calls == [operator_chat, operator_chat]
    assert "telemetry_pending" not in saved
    assert saved["telemetry_error"] == "visual_gateway_http_404"

    asyncio.run(monitor._tick(bot))
    assert calls == [operator_chat, operator_chat]


def test_telemetry_without_operator_chat_never_falls_back_to_admin_ids(monkeypatch):
    snapshot = limits.PlatformResourceSnapshot(
        configured=True,
        telemetry_available=False,
        base_url="http://visual-creative-gateway:8097",
        token_configured=True,
        error_code="visual_gateway_http_502",
    )
    saved: dict[str, object] = {
        "telemetry_pending": {
            "day": "2026-08-11",
            "error": "visual_gateway_http_502",
            "message": "legacy operator message",
            "pending_admin_ids": [101],
        }
    }
    calls: list[int] = []

    class Bot:
        async def send_message(self, chat_id: int, _text: str) -> None:
            calls.append(chat_id)

    monkeypatch.setattr(monitor, "get_platform_resource_snapshot", lambda: snapshot)
    monkeypatch.setattr(monitor, "_load_state", lambda: dict(saved))
    monkeypatch.setattr(monitor, "_save_state", lambda value: _replace_saved(saved, value))
    monkeypatch.setattr(monitor, "_resource_alert_chat_ids", lambda: ())

    asyncio.run(monitor._tick(Bot()))

    assert calls == []
    assert "telemetry_pending" not in saved
    assert saved["telemetry_error"] == "visual_gateway_http_502"


def test_operational_alerts_never_fall_back_to_user_admin_chat(monkeypatch):
    snapshot = _snapshot(used=29)
    saved: dict[str, object] = {}
    calls: list[int] = []

    class Bot:
        async def send_message(self, chat_id: int, _text: str) -> None:
            calls.append(chat_id)

    monkeypatch.setattr(monitor, "get_platform_resource_snapshot", lambda: snapshot)
    monkeypatch.setattr(monitor, "_load_state", lambda: dict(saved))
    monkeypatch.setattr(monitor, "_save_state", lambda value: _replace_saved(saved, value))
    monkeypatch.setattr(monitor, "_resource_alert_chat_ids", lambda: ())

    asyncio.run(monitor._tick(Bot()))

    assert calls == []
    assert "threshold_pending" not in saved
    assert saved["levels"]["image"] == 95


def test_provider_watch_is_silent_without_explicit_operator_chat(monkeypatch):
    calls: list[int] = []

    class Bot:
        async def send_message(self, chat_id: int, _text: str) -> None:
            calls.append(chat_id)

    monkeypatch.setattr(monitor, "_resource_alert_chat_ids", lambda: ())

    delivered = asyncio.run(
        monitor._deliver_provider_alerts(
            Bot(),
            alerts=["🔴 provider failure"],
        )
    )

    assert delivered is True
    assert calls == []


def test_resource_operator_chat_ids_accept_private_and_group_ids(monkeypatch):
    monkeypatch.setenv(
        "CLIENTPLATFORM_RESOURCE_ALERT_CHAT_IDS",
        "123, -100987654321, invalid, 0, 123",
    )

    assert monitor._resource_alert_chat_ids() == (-100987654321, 123)



def test_provider_watch_alerts_on_model_lifecycle_fallback_and_circuit():
    snapshot = provider_health.VisualProviderHealthSnapshot(
        available=True,
        configured_image=("yandexart", "gigachat"),
        configured_video=("yandexart_motion",),
        models={
            "yandexart": {
                "model": "art://folder/yandex-art-2.0",
                "model_id": "yandex-art-2.0",
                "deprecated_at": "2026-09-07",
                "days_remaining": -20,
                "replacement": "aliceai-image-art-3.0",
                "status": "deprecated",
            }
        },
        runtime={
            "image": {
                "provider": "gigachat",
                "model": "GigaChat-2-Pro",
                "error_code": "",
                "updated_at_epoch": 100,
                "failover": True,
            },
            "circuits_open_seconds": {"yandexart": 850},
        },
    )

    state, alerts = monitor._provider_state_and_alerts(snapshot, {})

    rendered = "\n".join(alerts)
    assert "Модель уже снята с поддержки" in rendered
    assert "aliceai-image-art-3.0" in rendered
    assert "Сработал автоматический fallback" in rendered
    assert "gigachat" in rendered
    assert "circuit breaker" in rendered
    assert state["runtime"]["image"]["provider"] == "gigachat"


def test_billing_watch_alerts_when_real_balance_crosses_threshold(monkeypatch):
    from decimal import Decimal

    monkeypatch.setenv("YANDEX_BILLING_ALERT_THRESHOLDS", "5000,2000,1000,500,100,0")
    snapshot = billing_health.YandexBillingSnapshot(
        configured=True,
        available=True,
        active=True,
        balance=Decimal("450"),
        currency="RUB",
    )

    state, alerts = monitor._billing_state_and_alerts(
        snapshot,
        {
            "configured": True,
            "available": True,
            "active": True,
            "balance": "1500",
            "currency": "RUB",
        },
    )

    rendered = "\n".join(alerts)
    assert "заканчиваются деньги" in rendered
    assert "450 RUB" in rendered
    assert "500 RUB" in rendered
    assert state["balance"] == "450"


def test_billing_watch_reports_telemetry_loss_and_recovery():
    lost = billing_health.YandexBillingSnapshot(
        configured=True,
        available=False,
        error_code="yandex_billing_http_401",
    )
    lost_state, alerts = monitor._billing_state_and_alerts(
        lost,
        {"configured": True, "available": True, "balance": "1000"},
    )
    assert "Billing telemetry недоступна" in "\n".join(alerts)

    from decimal import Decimal

    recovered = billing_health.YandexBillingSnapshot(
        configured=True,
        available=True,
        active=True,
        balance=Decimal("1000"),
        currency="RUB",
    )
    _state, recovery_alerts = monitor._billing_state_and_alerts(recovered, lost_state)
    assert "Billing telemetry восстановилась" in "\n".join(recovery_alerts)


def test_provider_watch_reports_catalog_loss_new_models_and_missing_current():
    previous = {
        "available": True,
        "configured_image": ["yandexart"],
        "configured_video": ["yandexart_motion"],
        "models": {
            "yandexart": {
                "model": "art://folder/aliceai-image-art-3.0",
                "model_id": "aliceai-image-art-3.0",
                "lifecycle_level": 0,
                "deprecated_at": "",
                "catalog_available": True,
                "configured_model_present": True,
                "catalog_error": "",
                "available_art_models": [
                    "art://folder/aliceai-image-art-3.0"
                ],
            }
        },
        "runtime": {},
        "circuits": {},
    }
    snapshot = provider_health.VisualProviderHealthSnapshot(
        available=True,
        configured_image=("yandexart",),
        configured_video=("yandexart_motion",),
        models={
            "yandexart": {
                "model": "art://folder/aliceai-image-art-3.0",
                "model_id": "aliceai-image-art-3.0",
                "deprecated_at": "",
                "days_remaining": None,
                "replacement": "",
                "status": "active",
                "catalog_available": True,
                "catalog_error": "",
                "configured_model_present": False,
                "available_art_models": (
                    "art://folder/aliceai-image-art-3.0",
                    "art://folder/aliceai-image-art-4.0",
                ),
            }
        },
        runtime={},
    )

    _state, alerts = monitor._provider_state_and_alerts(snapshot, previous)

    rendered = "\n".join(alerts)
    assert "появились новые image-модели" in rendered
    assert "aliceai-image-art-4.0" in rendered
    assert "отсутствует в каталоге" in rendered
    assert "не включается автоматически" in rendered


def test_provider_watch_reports_catalog_telemetry_failure_once():
    snapshot = provider_health.VisualProviderHealthSnapshot(
        available=True,
        configured_image=("yandexart",),
        configured_video=("yandexart_motion",),
        models={
            "yandexart": {
                "model": "art://folder/aliceai-image-art-3.0",
                "model_id": "aliceai-image-art-3.0",
                "status": "active",
                "catalog_available": False,
                "catalog_error": "yandex_models_http_403",
            }
        },
        runtime={},
    )

    current, alerts = monitor._provider_state_and_alerts(snapshot, {})
    assert "Каталог моделей Yandex AI Studio недоступен" in "\n".join(alerts)

    _same, repeated = monitor._provider_state_and_alerts(snapshot, current)
    assert repeated == []


def test_billing_watch_warns_once_for_static_iam_and_reports_renewable_upgrade():
    from decimal import Decimal

    static = billing_health.YandexBillingSnapshot(
        configured=True,
        available=True,
        active=True,
        balance=Decimal("3000"),
        currency="RUB",
        auth_mode="static_iam_token",
    )
    static_state, alerts = monitor._billing_state_and_alerts(static, {})
    assert "статический IAM-token" in "\n".join(alerts)

    _same, repeated = monitor._billing_state_and_alerts(static, static_state)
    assert repeated == []

    renewable = billing_health.YandexBillingSnapshot(
        configured=True,
        available=True,
        active=True,
        balance=Decimal("3000"),
        currency="RUB",
        auth_mode="authorized_key",
        auth_expires_at_epoch=9999999999.0,
    )
    _state, upgraded = monitor._billing_state_and_alerts(renewable, static_state)
    assert "автоматически обновляемый IAM-token" in "\n".join(upgraded)


def test_provider_watch_reports_model_change_and_recovery_once():
    previous = {
        "available": False,
        "configured_image": [],
        "configured_video": [],
        "models": {
            "yandexart": {
                "model": "art://folder/yandex-art-2.0",
                "model_id": "yandex-art-2.0",
                "lifecycle_level": 5,
                "deprecated_at": "2026-09-07",
            }
        },
        "runtime": {},
        "circuits": {},
    }
    snapshot = provider_health.VisualProviderHealthSnapshot(
        available=True,
        configured_image=("yandexart",),
        configured_video=("yandexart_motion",),
        models={
            "yandexart": {
                "model": "art://folder/aliceai-image-art-3.0",
                "model_id": "aliceai-image-art-3.0",
                "deprecated_at": "",
                "days_remaining": None,
                "replacement": "",
                "status": "active",
            }
        },
        runtime={},
    )

    current, alerts = monitor._provider_state_and_alerts(snapshot, previous)
    rendered = "\n".join(alerts)
    assert "Gateway восстановился" in rendered
    assert "Модель генерации автоматически/операторски изменена" in rendered
    assert "aliceai-image-art-3.0" in rendered

    _again, repeated = monitor._provider_state_and_alerts(snapshot, current)
    assert repeated == []


def test_monitor_loop_survives_database_driver_error(monkeypatch):
    calls = 0

    async def broken_tick(_bot):
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("temporary database outage")

    async def stop_after_retry_window(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(monitor, "_tick", broken_tick)
    monkeypatch.setattr(monitor.asyncio, "sleep", stop_after_retry_window)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(monitor._monitor_loop(object()))
    assert calls == 1
    assert monitor.platform_resource_monitor_snapshot()["last_error"] == (
        "platform_resource_monitor_tick_failed"
    )

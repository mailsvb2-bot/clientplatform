from __future__ import annotations

import asyncio

from clientplatform.runtime import platform_resource_monitor as monitor
from services import platform_resource_limits as limits


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
    monkeypatch.setattr(monitor, "_save_state", lambda value: saved.update(value))

    async def fake_send(_bot, text: str) -> int:
        sent.append(text)
        return 1

    monkeypatch.setattr(monitor, "_send_superadmins", fake_send)

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
    monkeypatch.setattr(monitor, "_save_state", lambda value: saved.clear() or saved.update(value))

    async def fake_send(_bot, text: str) -> int:
        sent.append(text)
        return 1

    monkeypatch.setattr(monitor, "_send_superadmins", fake_send)

    asyncio.run(monitor._tick(object()))
    assert len(sent) == 1
    assert "порог 95%" in sent[-1]

    asyncio.run(monitor._tick(object()))
    assert len(sent) == 1
    assert saved["levels"]["image"] == 0

    asyncio.run(monitor._tick(object()))
    assert len(sent) == 2
    assert "порог 85%" in sent[-1]

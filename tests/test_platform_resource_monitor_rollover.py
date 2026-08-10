from __future__ import annotations

import asyncio

from clientplatform.runtime import platform_resource_monitor as monitor
from services import platform_resource_limits as limits


def test_pending_threshold_delivery_survives_utc_day_rollover(monkeypatch):
    snapshot = limits.PlatformResourceSnapshot(
        configured=True,
        telemetry_available=True,
        base_url="http://visual-creative-gateway:8097",
        token_configured=True,
        day_utc="2026-08-12",
        resets_at="2026-08-13T00:00:00Z",
        usage_semantics="gateway_reservations_not_provider_billing",
        jobs=limits.ResourceCounter(used=0, limit=30, remaining=30),
        image=limits.ResourceCounter(used=0, limit=30, remaining=30),
        video=limits.ResourceCounter(used=0, limit=30, remaining=30),
        active=limits.ResourceCounter(used=0, limit=3, remaining=3),
    )
    saved: dict[str, object] = {
        "day_utc": "2026-08-11",
        "levels": {"jobs": 85, "image": 85, "video": 0, "active": 0},
        "threshold_pending": {
            "day": "2026-08-11",
            "message": "yesterday threshold alert",
            "pending_admin_ids": [202],
            "target_levels": {"jobs": 95, "image": 95, "video": 0, "active": 0},
        },
    }
    calls: list[tuple[int, str]] = []

    class Bot:
        async def send_message(self, admin_id: int, text: str) -> None:
            calls.append((admin_id, text))

    def save(value):
        saved.clear()
        saved.update(value)

    monkeypatch.setattr(monitor, "get_platform_resource_snapshot", lambda: snapshot)
    monkeypatch.setattr(monitor, "_load_state", lambda: dict(saved))
    monkeypatch.setattr(monitor, "_save_state", save)
    monkeypatch.setattr(monitor, "_superadmin_ids", lambda: (101, 202))

    asyncio.run(monitor._tick(Bot()))

    assert calls == [(202, "yesterday threshold alert")]
    assert "threshold_pending" not in saved
    assert saved["day_utc"] == "2026-08-12"
    assert saved["levels"] == {"jobs": 0, "image": 0, "video": 0, "active": 0}

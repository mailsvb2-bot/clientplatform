from __future__ import annotations

import asyncio

from clientplatform.runtime import platform_resource_monitor as monitor
from services import platform_resource_limits as limits
from services import visual_provider_health as provider_health


def _stable_provider_snapshot():
    return provider_health.VisualProviderHealthSnapshot(
        available=True,
        configured_image=("yandexart",),
        configured_video=("selfhosted",),
        configured_video_native=("selfhosted",),
        configured_video_motion=(),
        video_generation_mode="native",
    )


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
    monkeypatch.setattr(
        monitor,
        "get_visual_provider_health_snapshot",
        _stable_provider_snapshot,
    )
    monkeypatch.setattr(monitor, "_load_state", lambda: dict(saved))
    monkeypatch.setattr(monitor, "_save_state", save)
    monkeypatch.setattr(monitor, "_resource_alert_chat_ids", lambda: (202,))

    asyncio.run(monitor._tick(Bot()))

    assert calls == [(202, "yesterday threshold alert")]
    assert "threshold_pending" not in saved
    assert saved["day_utc"] == "2026-08-12"
    assert saved["levels"] == {"jobs": 0, "image": 0, "video": 0, "active": 0}


def test_provider_monitor_alerts_when_only_motion_fallback_remains():
    previous = {
        "available": True,
        "configured_image": ["yandexart"],
        "configured_video": ["selfhosted", "yandexart_motion"],
        "configured_video_native": ["selfhosted"],
        "configured_video_motion": ["yandexart_motion"],
        "video_generation_mode": "native",
        "models": {},
        "runtime": {},
        "circuits": {},
    }
    snapshot = provider_health.VisualProviderHealthSnapshot(
        available=True,
        configured_image=("yandexart",),
        configured_video=("yandexart_motion",),
        configured_video_native=(),
        configured_video_motion=("yandexart_motion",),
        video_generation_mode="motion",
    )

    current, alerts = monitor._provider_state_and_alerts(snapshot, previous)

    assert current["video_generation_mode"] == "motion"
    assert current["configured_video_native"] == []
    assert current["configured_video_motion"] == ["yandexart_motion"]
    assert any("Полноценная AI-генерация видео недоступна" in item for item in alerts)


def test_provider_monitor_initial_motion_baseline_is_silent():
    snapshot = provider_health.VisualProviderHealthSnapshot(
        available=True,
        configured_image=("yandexart",),
        configured_video=("yandexart_motion",),
        configured_video_native=(),
        configured_video_motion=("yandexart_motion",),
        video_generation_mode="motion",
    )

    current, alerts = monitor._provider_state_and_alerts(snapshot, {})

    assert current["video_generation_mode"] == "motion"
    assert alerts == []


def test_provider_monitor_alerts_when_native_video_recovers():
    previous = {
        "available": True,
        "configured_image": ["yandexart"],
        "configured_video": ["yandexart_motion"],
        "configured_video_native": [],
        "configured_video_motion": ["yandexart_motion"],
        "video_generation_mode": "motion",
        "models": {},
        "runtime": {},
        "circuits": {},
    }
    snapshot = provider_health.VisualProviderHealthSnapshot(
        available=True,
        configured_image=("yandexart",),
        configured_video=("selfhosted", "yandexart_motion"),
        configured_video_native=("selfhosted",),
        configured_video_motion=("yandexart_motion",),
        video_generation_mode="native",
    )

    current, alerts = monitor._provider_state_and_alerts(snapshot, previous)

    assert current["video_generation_mode"] == "native"
    assert any("AI-генерация видео восстановлена" in item for item in alerts)

def test_provider_monitor_preserves_last_known_topology_during_gateway_outage():
    previous = {
        "available": True,
        "configured_image": ["yandexart"],
        "configured_video": ["yandexart_motion"],
        "configured_video_native": [],
        "configured_video_motion": ["yandexart_motion"],
        "video_generation_mode": "motion",
        "models": {
            "yandexart": {
                "model": "art://folder/aliceai-image-art-3.0",
                "catalog_available": False,
                "catalog_error": "yandex_models_http_403",
                "configured_model_present": False,
                "available_art_models": [],
                "lifecycle_level": 0,
                "deprecated_at": "",
                "model_id": "aliceai-image-art-3.0",
            }
        },
        "runtime": {"image": {"provider": "yandexart", "error_code": ""}},
        "circuits": {"yandexart": 60},
    }
    snapshot = provider_health.VisualProviderHealthSnapshot(
        available=False,
        error_code="visual_gateway_transport_URLError",
    )

    current, alerts = monitor._provider_state_and_alerts(snapshot, previous)

    assert current["available"] is False
    assert current["configured_image"] == ["yandexart"]
    assert current["configured_video"] == ["yandexart_motion"]
    assert current["video_generation_mode"] == "motion"
    assert current["models"] == previous["models"]
    assert current["runtime"] == previous["runtime"]
    assert current["circuits"] == previous["circuits"]
    assert len(alerts) == 1
    assert "Visual Provider Gateway недоступен" in alerts[0]


def test_provider_monitor_recovery_does_not_repeat_catalog_or_provider_restored_noise():
    previous = {
        "available": False,
        "error_code": "visual_gateway_transport_URLError",
        "configured_image": ["yandexart"],
        "configured_video": ["yandexart_motion"],
        "configured_video_native": [],
        "configured_video_motion": ["yandexart_motion"],
        "video_generation_mode": "motion",
        "models": {
            "yandexart": {
                "model": "art://folder/aliceai-image-art-3.0",
                "model_id": "aliceai-image-art-3.0",
                "lifecycle_level": 0,
                "deprecated_at": "",
                "catalog_available": False,
                "configured_model_present": False,
                "catalog_error": "yandex_models_http_403",
                "available_art_models": [],
            }
        },
        "runtime": {},
        "circuits": {},
    }
    snapshot = provider_health.VisualProviderHealthSnapshot(
        available=True,
        configured_image=("yandexart",),
        configured_video=("yandexart_motion",),
        configured_video_native=(),
        configured_video_motion=("yandexart_motion",),
        video_generation_mode="motion",
        models={
            "yandexart": {
                "model": "art://folder/aliceai-image-art-3.0",
                "model_id": "aliceai-image-art-3.0",
                "days_remaining": None,
                "status": "active",
                "deprecated_at": "",
                "replacement": "",
                "catalog_available": False,
                "configured_model_present": False,
                "catalog_error": "yandex_models_http_403",
                "available_art_models": (),
            }
        },
    )

    current, alerts = monitor._provider_state_and_alerts(snapshot, previous)

    assert current["available"] is True
    assert alerts == ["🟢 Visual Provider Gateway восстановился."]


def test_provider_monitor_catalog_403_explains_catalog_scope_without_declaring_generation_dead():
    previous = {
        "available": True,
        "configured_image": ["yandexart"],
        "configured_video": ["yandexart_motion"],
        "configured_video_native": [],
        "configured_video_motion": ["yandexart_motion"],
        "video_generation_mode": "motion",
        "models": {},
        "runtime": {},
        "circuits": {},
    }
    snapshot = provider_health.VisualProviderHealthSnapshot(
        available=True,
        configured_image=("yandexart",),
        configured_video=("yandexart_motion",),
        configured_video_motion=("yandexart_motion",),
        video_generation_mode="motion",
        models={
            "yandexart": {
                "model": "art://folder/aliceai-image-art-3.0",
                "model_id": "aliceai-image-art-3.0",
                "days_remaining": None,
                "status": "active",
                "deprecated_at": "",
                "replacement": "",
                "catalog_available": False,
                "configured_model_present": False,
                "catalog_error": "yandex_models_http_403",
                "available_art_models": (),
            }
        },
    )

    _current, alerts = monitor._provider_state_and_alerts(snapshot, previous)

    message = "\n".join(alerts)
    assert "scope API-ключа" in message
    assert "не означает, что генерация изображений сломана" in message

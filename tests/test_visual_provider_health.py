from __future__ import annotations

from services import visual_provider_health as health


def test_tuple_normalization_filters_empty_values():
    assert health._tuple_of_strings(None) == ()
    assert health._tuple_of_strings({"x": 1}) == ()
    assert health._tuple_of_strings(["yandexart", "", "  ", 42]) == (
        "yandexart",
        "42",
    )


def test_provider_health_fails_closed_when_gateway_is_not_configured(monkeypatch):
    monkeypatch.setattr(
        health.visual_gateway,
        "gateway_snapshot",
        lambda: {"configured": False, "token_configured": False},
    )

    snapshot = health.get_visual_provider_health_snapshot()

    assert snapshot.available is False
    assert snapshot.error_code == "visual_gateway_not_configured"


def test_provider_health_fails_closed_when_gateway_request_fails(monkeypatch):
    monkeypatch.setattr(
        health.visual_gateway,
        "gateway_snapshot",
        lambda: {"configured": True, "token_configured": True},
    )

    def broken(*_args, **_kwargs):
        raise health.VisualCreativeGatewayError("")

    monkeypatch.setattr(health.visual_gateway, "_json", broken)

    snapshot = health.get_visual_provider_health_snapshot()

    assert snapshot.available is False
    assert snapshot.error_code == "visual_provider_health_unavailable"


def test_provider_health_preserves_safe_runtime_and_model_metadata(monkeypatch):
    monkeypatch.setattr(
        health.visual_gateway,
        "gateway_snapshot",
        lambda: {"configured": True, "token_configured": True},
    )
    monkeypatch.setattr(
        health.visual_gateway,
        "_json",
        lambda *_args, **_kwargs: {
            "configured_image": ["yandexart", "", "gigachat"],
            "configured_video": ("yandexart_motion",),
            "image_order": ["yandexart", "gigachat"],
            "video_order": ["yandexart_motion", "selfhosted"],
            "models": {
                "yandexart": {
                    "model": "art://folder/aliceai-image-art-3.0",
                    "status": "active",
                }
            },
            "runtime": {
                "image": {"provider": "yandexart"},
                "circuits_open_seconds": {},
            },
        },
    )

    snapshot = health.get_visual_provider_health_snapshot()

    assert snapshot.available is True
    assert snapshot.configured_image == ("yandexart", "gigachat")
    assert snapshot.configured_video == ("yandexart_motion",)
    assert snapshot.image_order == ("yandexart", "gigachat")
    assert snapshot.video_order == ("yandexart_motion", "selfhosted")
    assert snapshot.models["yandexart"]["status"] == "active"
    assert snapshot.runtime["image"]["provider"] == "yandexart"


def test_provider_health_drops_invalid_metadata_shapes(monkeypatch):
    monkeypatch.setattr(
        health.visual_gateway,
        "gateway_snapshot",
        lambda: {"configured": True, "token_configured": True},
    )
    monkeypatch.setattr(
        health.visual_gateway,
        "_json",
        lambda *_args, **_kwargs: {
            "configured_image": "yandexart",
            "configured_video": None,
            "image_order": None,
            "video_order": {},
            "models": [],
            "runtime": "bad",
        },
    )

    snapshot = health.get_visual_provider_health_snapshot()

    assert snapshot.available is True
    assert snapshot.configured_image == ()
    assert snapshot.configured_video == ()
    assert snapshot.models == {}
    assert snapshot.runtime == {}

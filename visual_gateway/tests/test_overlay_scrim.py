from __future__ import annotations

from visual_gateway.server import GatewayConfig, _overlay


def _config(tmp_path):
    return GatewayConfig(
        token="t" * 32,
        upstream_url="http://127.0.0.1:9999",
        upstream_token="",
        state_dir=tmp_path / "state",
        asset_dir=tmp_path / "assets",
    )


def test_overlay_uses_gradient_without_opaque_plate(tmp_path):
    composition = {
        "brand": {
            "primary_color": "#172033",
            "accent_color": "#E9C46A",
            "text_color": "#FFFFFF",
        },
        "headline": "Новая услуга",
        "body": "Подробности",
        "cta": "Подробнее",
        "layout": "lower_card",
    }
    layer = _overlay(_config(tmp_path), 1080, 1350, composition)
    assert layer.size == (1080, 1350)
    assert layer.getpixel((5, 5))[3] == 0
    assert layer.getpixel((5, 1345))[3] > 100
    assert layer.getpixel((5, 900))[3] < layer.getpixel((5, 1345))[3]

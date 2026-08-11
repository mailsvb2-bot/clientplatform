from __future__ import annotations

import hashlib

import pytest

import services.visual_creative_gateway as gateway
from services.visual_creative_gateway import VisualCreativeGatewayError, VisualCreativeJob


def _job():
    return VisualCreativeJob("job1", "fake", "business-a", "image", "succeeded", asset_ready=True)


def _response(scope="business-a"):
    return {
        "id": "pack1",
        "scope_id": scope,
        "source_job_id": "job1",
        "status": "succeeded",
        "error_code": "",
        "assets": [{
            "format_id": "feed", "kind": "image", "width": 1080, "height": 1350,
            "mime_type": "image/jpeg", "sha256": hashlib.sha256(b"image").hexdigest(),
            "asset_ready": True, "quality": {"technical_score": 100},
        }],
    }


def test_render_pack_is_scope_bound_and_normalizes_formats(monkeypatch):
    calls=[]
    def fake_json(method, path, *, payload=None, timeout_seconds=None):
        calls.append((method,path,payload,timeout_seconds)); return _response()
    monkeypatch.setattr(gateway, "_json", fake_json)
    pack=gateway.render_visual_pack(_job(), formats=("feed","feed"), composition={"headline":"x"}, idempotency_key="clientplatform:v1:render")
    assert pack.scope_id == "business-a"
    assert calls[0][2]["formats"] == ["feed"]
    assert calls[0][2]["scope_id"] == "business-a"


def test_render_pack_rejects_cross_scope_response(monkeypatch):
    monkeypatch.setattr(gateway, "_json", lambda *a, **k: _response("business-b"))
    with pytest.raises(VisualCreativeGatewayError, match="render_pack"):
        gateway.render_visual_pack(_job(), formats=("feed",), composition={}, idempotency_key="clientplatform:v1:render")


def test_render_download_verifies_digest(monkeypatch, tmp_path):
    pack=gateway._render_pack(_response(), expected_scope_id="business-a", expected_source_job_id="job1")
    monkeypatch.setattr(gateway, "_request", lambda *a, **k: ({"content-type":"image/jpeg"}, b"image"))
    path=gateway.download_render_asset(pack, "feed", output_dir=str(tmp_path))
    assert path.read_bytes() == b"image"
    monkeypatch.setattr(gateway, "_request", lambda *a, **k: ({"content-type":"image/jpeg"}, b"tampered"))
    with pytest.raises(VisualCreativeGatewayError, match="digest_mismatch"):
        gateway.download_render_asset(pack, "feed", output_dir=str(tmp_path))


def test_succeeded_render_pack_requires_exact_format_set_kind_dimensions_and_digest(monkeypatch):
    missing_digest = _response()
    missing_digest["assets"][0]["sha256"] = ""
    monkeypatch.setattr(gateway, "_json", lambda *a, **k: missing_digest)
    with pytest.raises(VisualCreativeGatewayError, match="render_asset"):
        gateway.render_visual_pack(_job(), formats=("feed",), composition={}, idempotency_key="clientplatform:v1:render")

    wrong_size = _response()
    wrong_size["assets"][0]["width"] = 1200
    monkeypatch.setattr(gateway, "_json", lambda *a, **k: wrong_size)
    with pytest.raises(VisualCreativeGatewayError, match="render_asset"):
        gateway.render_visual_pack(_job(), formats=("feed",), composition={}, idempotency_key="clientplatform:v1:render")

    wrong_kind = _response()
    wrong_kind["assets"][0]["kind"] = "video"
    wrong_kind["assets"][0]["mime_type"] = "video/mp4"
    monkeypatch.setattr(gateway, "_json", lambda *a, **k: wrong_kind)
    with pytest.raises(VisualCreativeGatewayError, match="render_asset"):
        gateway.render_visual_pack(_job(), formats=("feed",), composition={}, idempotency_key="clientplatform:v1:render")

    incomplete = _response()
    monkeypatch.setattr(gateway, "_json", lambda *a, **k: incomplete)
    with pytest.raises(VisualCreativeGatewayError, match="incomplete_render_pack"):
        gateway.render_visual_pack(_job(), formats=("feed", "story"), composition={}, idempotency_key="clientplatform:v1:render")

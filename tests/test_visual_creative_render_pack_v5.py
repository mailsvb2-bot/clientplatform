from __future__ import annotations

import hashlib

import pytest

import services.visual_creative_gateway as gateway
from services.visual_creative_gateway import VisualCreativeGatewayError, VisualCreativeJob


def _job(
    *,
    status: str = "succeeded",
    ready: bool = True,
    kind: str = "image",
) -> VisualCreativeJob:
    return VisualCreativeJob(
        "job1",
        "fake",
        "business-a",
        kind,
        status,
        asset_ready=ready,
    )


def _asset(
    *,
    format_id: str = "feed",
    kind: str = "image",
    width: int = 1080,
    height: int = 1350,
    mime_type: str = "image/jpeg",
    sha256: str | None = None,
    ready: bool = True,
    quality: object = None,
) -> dict[str, object]:
    return {
        "format_id": format_id,
        "kind": kind,
        "width": width,
        "height": height,
        "mime_type": mime_type,
        "sha256": hashlib.sha256(b"image").hexdigest() if sha256 is None else sha256,
        "asset_ready": ready,
        "quality": {"technical_score": 100} if quality is None else quality,
    }


def _response(scope: str = "business-a") -> dict[str, object]:
    return {
        "id": "pack1",
        "scope_id": scope,
        "source_job_id": "job1",
        "status": "succeeded",
        "error_code": "",
        "assets": [_asset()],
    }


def test_render_pack_is_scope_bound_and_normalizes_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    def fake_json(method, path, *, payload=None, timeout_seconds=None):
        calls.append((method, path, payload, timeout_seconds))
        return _response()

    monkeypatch.setattr(gateway, "_json", fake_json)
    pack = gateway.render_visual_pack(
        _job(),
        formats=("feed", "feed"),
        composition={"headline": "x"},
        idempotency_key="clientplatform:v1:render",
    )
    assert pack.scope_id == "business-a"
    assert calls[0][2]["formats"] == ["feed"]  # type: ignore[index]
    assert calls[0][2]["scope_id"] == "business-a"  # type: ignore[index]


def test_render_pack_rejects_cross_scope_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "_json", lambda *a, **k: _response("business-b"))
    with pytest.raises(VisualCreativeGatewayError, match="render_pack"):
        gateway.render_visual_pack(
            _job(),
            formats=("feed",),
            composition={},
            idempotency_key="clientplatform:v1:render",
        )


def test_render_download_verifies_digest(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    pack = gateway._render_pack(
        _response(),
        expected_scope_id="business-a",
        expected_source_job_id="job1",
    )
    monkeypatch.setattr(
        gateway,
        "_request",
        lambda *a, **k: ({"content-type": "image/jpeg"}, b"image"),
    )
    path = gateway.download_render_asset(pack, "feed", output_dir=str(tmp_path))
    assert path.read_bytes() == b"image"

    monkeypatch.setattr(
        gateway,
        "_request",
        lambda *a, **k: ({"content-type": "image/jpeg"}, b"tampered"),
    )
    with pytest.raises(VisualCreativeGatewayError, match="digest_mismatch"):
        gateway.download_render_asset(pack, "feed", output_dir=str(tmp_path))


def test_succeeded_render_pack_requires_exact_format_set_kind_dimensions_and_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_digest = _response()
    missing_digest["assets"][0]["sha256"] = ""  # type: ignore[index]
    monkeypatch.setattr(gateway, "_json", lambda *a, **k: missing_digest)
    with pytest.raises(VisualCreativeGatewayError, match="render_asset"):
        gateway.render_visual_pack(
            _job(),
            formats=("feed",),
            composition={},
            idempotency_key="clientplatform:v1:render",
        )

    wrong_size = _response()
    wrong_size["assets"][0]["width"] = 1200  # type: ignore[index]
    monkeypatch.setattr(gateway, "_json", lambda *a, **k: wrong_size)
    with pytest.raises(VisualCreativeGatewayError, match="render_asset"):
        gateway.render_visual_pack(
            _job(),
            formats=("feed",),
            composition={},
            idempotency_key="clientplatform:v1:render",
        )

    wrong_kind = _response()
    wrong_kind["assets"][0]["kind"] = "video"  # type: ignore[index]
    wrong_kind["assets"][0]["mime_type"] = "video/mp4"  # type: ignore[index]
    monkeypatch.setattr(gateway, "_json", lambda *a, **k: wrong_kind)
    with pytest.raises(VisualCreativeGatewayError, match="render_asset"):
        gateway.render_visual_pack(
            _job(),
            formats=("feed",),
            composition={},
            idempotency_key="clientplatform:v1:render",
        )

    incomplete = _response()
    monkeypatch.setattr(gateway, "_json", lambda *a, **k: incomplete)
    with pytest.raises(VisualCreativeGatewayError, match="incomplete_render_pack"):
        gateway.render_visual_pack(
            _job(),
            formats=("feed", "story"),
            composition={},
            idempotency_key="clientplatform:v1:render",
        )


def test_running_render_pack_allows_partial_asset_but_rejects_unexpected_format() -> None:
    running = _response()
    running["status"] = "running"
    running["assets"] = [
        _asset(sha256="", ready=False, quality="provider-pending")
    ]
    pack = gateway._render_pack(
        running,
        expected_scope_id="business-a",
        expected_source_job_id="job1",
        expected_formats=("feed",),
        expected_kind="image",
    )
    assert pack.status == "running"
    assert pack.assets[0].asset_ready is False
    assert pack.assets[0].sha256 == ""
    assert pack.assets[0].quality == {}

    with pytest.raises(VisualCreativeGatewayError, match="unexpected_render_format"):
        gateway._render_pack(
            running,
            expected_scope_id="business-a",
            expected_source_job_id="job1",
            expected_formats=("story",),
            expected_kind="image",
        )


def test_render_pack_rejects_invalid_expected_contract_and_empty_success() -> None:
    with pytest.raises(VisualCreativeGatewayError, match="invalid_render_pack"):
        gateway._render_pack(
            _response(),
            expected_scope_id="business-a",
            expected_source_job_id="job1",
            expected_formats=("poster",),
        )

    with pytest.raises(VisualCreativeGatewayError, match="invalid_render_pack"):
        gateway._render_pack(
            _response(),
            expected_scope_id="business-a",
            expected_source_job_id="job1",
            expected_kind="audio",
        )

    non_mapping_asset = _response()
    non_mapping_asset["assets"] = ["not-an-asset"]
    with pytest.raises(VisualCreativeGatewayError, match="invalid_render_asset"):
        gateway._render_pack(
            non_mapping_asset,
            expected_scope_id="business-a",
            expected_source_job_id="job1",
        )

    empty_success = _response()
    empty_success["assets"] = []
    with pytest.raises(VisualCreativeGatewayError, match="incomplete_render_pack"):
        gateway._render_pack(
            empty_success,
            expected_scope_id="business-a",
            expected_source_job_id="job1",
        )


@pytest.mark.parametrize(
    "assets",
    [
        [_asset(format_id="poster")],
        [_asset(), _asset()],
        [_asset(kind="audio")],
        [_asset(ready="yes")],  # type: ignore[arg-type]
        [_asset(mime_type="")],
        [_asset(mime_type="image/jpeg; charset=binary")],
        [_asset(mime_type="video/mp4")],
        [_asset(ready=False)],
    ],
)
def test_succeeded_render_pack_rejects_each_asset_contract_violation(
    assets: list[dict[str, object]],
) -> None:
    response = _response()
    response["assets"] = assets
    with pytest.raises(VisualCreativeGatewayError, match="invalid_render_asset"):
        gateway._render_pack(
            response,
            expected_scope_id="business-a",
            expected_source_job_id="job1",
            expected_kind="image",
        )


def test_running_render_pack_rejects_malformed_optional_digest() -> None:
    response = _response()
    response["status"] = "running"
    response["assets"] = [_asset(sha256="not-a-sha", ready=False)]
    with pytest.raises(VisualCreativeGatewayError, match="invalid_render_asset"):
        gateway._render_pack(
            response,
            expected_scope_id="business-a",
            expected_source_job_id="job1",
            expected_formats=("feed",),
        )


def test_render_visual_pack_rejects_invalid_inputs() -> None:
    with pytest.raises(VisualCreativeGatewayError, match="source_not_ready"):
        gateway.render_visual_pack(
            _job(status="running", ready=False),
            formats=("feed",),
            composition={},
            idempotency_key="clientplatform:v1:render",
        )

    with pytest.raises(ValueError, match="invalid_visual_render_format"):
        gateway.render_visual_pack(
            _job(),
            formats=("poster",),
            composition={},
            idempotency_key="clientplatform:v1:render",
        )

    with pytest.raises(ValueError, match="visual_render_formats_required"):
        gateway.render_visual_pack(
            _job(),
            formats=(),
            composition={},
            idempotency_key="clientplatform:v1:render",
        )

    with pytest.raises(ValueError, match="idempotency"):
        gateway.render_visual_pack(
            _job(),
            formats=("feed",),
            composition={},
            idempotency_key="short",
        )

    with pytest.raises(ValueError, match="composition"):
        gateway.render_visual_pack(
            _job(),
            formats=("feed",),
            composition=[],  # type: ignore[arg-type]
            idempotency_key="clientplatform:v1:render",
        )


def test_render_download_rejects_not_ready_invalid_pack_and_wrong_media_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    running = gateway.VisualRenderPack(
        id="pack1",
        scope_id="business-a",
        source_job_id="job1",
        status="running",
        error_code="",
        assets=(),
    )
    with pytest.raises(VisualCreativeGatewayError, match="content_not_ready"):
        gateway.download_render_asset(running, "feed", output_dir=str(tmp_path))

    asset = gateway.VisualRenderAsset(
        format_id="feed",
        kind="image",
        width=1080,
        height=1350,
        mime_type="image/jpeg",
        sha256="",
        asset_ready=True,
        quality={},
    )
    invalid_pack = gateway.VisualRenderPack(
        id="bad pack id",
        scope_id="business-a",
        source_job_id="job1",
        status="succeeded",
        error_code="",
        assets=(asset,),
    )
    with pytest.raises(VisualCreativeGatewayError, match="invalid_render_asset"):
        gateway.download_render_asset(invalid_pack, "feed", output_dir=str(tmp_path))

    valid_pack = gateway.VisualRenderPack(
        id="pack1",
        scope_id="business-a",
        source_job_id="job1",
        status="succeeded",
        error_code="",
        assets=(asset,),
    )
    monkeypatch.setattr(
        gateway,
        "_request",
        lambda *a, **k: ({"content-type": "video/mp4"}, b"image"),
    )
    with pytest.raises(VisualCreativeGatewayError, match="unexpected_render_media_type"):
        gateway.download_render_asset(valid_pack, "feed", output_dir=str(tmp_path))


def test_render_download_normalizes_jpeg_suffix_and_cleans_failed_materialization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    pack = gateway._render_pack(
        _response(),
        expected_scope_id="business-a",
        expected_source_job_id="job1",
    )
    monkeypatch.setattr(
        gateway,
        "_request",
        lambda *a, **k: ({"content-type": "image/jpeg"}, b"image"),
    )
    monkeypatch.setattr(gateway.mimetypes, "guess_extension", lambda mime: ".jpe")
    path = gateway.download_render_asset(pack, "feed", output_dir=str(tmp_path))
    assert path.suffix == ".jpg"

    def fail_replace(source, target) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(gateway.os, "replace", fail_replace)
    with pytest.raises(VisualCreativeGatewayError, match="materialization_failed"):
        gateway.download_render_asset(pack, "feed", output_dir=str(tmp_path))
    assert not list(tmp_path.glob("*.tmp"))


def test_download_visual_normalizes_legacy_jpeg_suffix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        gateway,
        "_request",
        lambda *a, **k: ({"content-type": "image/jpeg"}, b"image"),
    )
    monkeypatch.setattr(gateway.mimetypes, "guess_extension", lambda mime: ".jpe")
    path = gateway.download_visual(_job(), output_dir=str(tmp_path))
    assert path.suffix == ".jpg"
    assert path.read_bytes() == b"image"

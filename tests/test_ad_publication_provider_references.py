from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from clientplatform.application import ad_goal_publication
from clientplatform.application import ad_publication_assets as assets
from clientplatform.domain.ad_publication_assets import (
    AdPublicationAsset,
    AdPublicationAssetKind,
    AdPublicationAssetSource,
)


def _jpeg() -> bytes:
    output = BytesIO()
    Image.new("RGB", (320, 240), "white").save(output, format="JPEG")
    return output.getvalue()


def _asset(*, provider_image_hash: str | None = "hash-1"):
    return AdPublicationAsset(
        publication_job_id=str(uuid4()),
        business_id=str(uuid4()),
        kind=AdPublicationAssetKind.IMAGE,
        source=AdPublicationAssetSource.UPLOAD,
        content_type="image/jpeg",
        original_name="photo.jpg",
        sha256="a" * 64,
        size_bytes=123,
        created_by_member_id=str(uuid4()),
        created_at="2026-09-29T00:00:00+00:00",
        updated_at="2026-09-29T00:00:00+00:00",
        provider_image_hash=provider_image_hash,
    )


def test_advertising_asset_domain_has_no_local_storage_field() -> None:
    assert "storage_path" not in AdPublicationAsset.__dataclass_fields__
    with pytest.raises(ValueError, match="provider reference is required"):
        _asset(provider_image_hash=None)


def test_image_bytes_upload_immediately_and_persist_only_provider_reference(monkeypatch) -> None:
    captured: dict[str, object] = {}
    actor = SimpleNamespace(business_id=str(uuid4()))
    publication_job_id = str(uuid4())
    job = SimpleNamespace(
        id=publication_job_id,
        business_id=actor.business_id,
        connection_id=str(uuid4()),
    )

    class Provider:
        def upload_image(self, *, access_token: str, payload: bytes, name: str) -> str:
            captured["access_token"] = access_token
            captured["payload"] = payload
            captured["name"] = name
            return "provider-image-hash"

    class Repository:
        def __init__(self, _conn) -> None:
            pass

        def replace(self, **kwargs):
            captured["replace"] = kwargs
            return AdPublicationAsset(
                    publication_job_id=publication_job_id,
                    business_id=actor.business_id,
                    kind=kwargs["kind"],
                    source=kwargs["source"],
                    content_type=kwargs["content_type"],
                    original_name=kwargs["original_name"],
                    sha256=kwargs["sha256"],
                    size_bytes=kwargs["size_bytes"],
                    duration_seconds=kwargs["duration_seconds"],
                    provider_image_hash=kwargs["provider_image_hash"],
                    created_by_member_id=str(uuid4()),
                    created_at="2026-09-29T00:00:00+00:00",
                    updated_at="2026-09-29T00:00:00+00:00",
                )

    @contextmanager
    def fake_db():
        yield object()

    provider = Provider()
    vault = object()
    monkeypatch.setattr(assets, "_publication_job", lambda **_kwargs: job)
    monkeypatch.setattr(assets, "get_db", fake_db)
    monkeypatch.setattr(assets, "AdPublicationAssetRepository", Repository)
    monkeypatch.setattr(
        assets,
        "with_access_token",
        lambda *, operation, **_kwargs: operation("access-token"),
    )

    result = assets.attach_image_bytes(
        actor=actor,
        publication_job_id=publication_job_id,
        payload=_jpeg(),
        original_name="owner-photo.png",
        provider=provider,
        vault=vault,
    )

    uploaded = captured["payload"]
    assert isinstance(uploaded, bytes)
    assert uploaded.startswith(b"\xff\xd8\xff")
    assert captured["access_token"] == "access-token"
    replace = captured["replace"]
    assert isinstance(replace, dict)
    assert "storage_path" not in replace
    assert replace["provider_image_hash"] == "provider-image-hash"
    assert result.provider_image_hash == "provider-image-hash"


def test_publication_worker_attaches_existing_hash_without_media_bytes(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    asset = _asset()
    monkeypatch.setattr(
        ad_goal_publication,
        "get_asset_for_worker",
        lambda **_kwargs: asset,
    )

    class Provider:
        def clear_media(self, **_kwargs):
            raise AssertionError("valid provider reference must not be cleared")

        def upload_image(self, **_kwargs):
            raise AssertionError("worker must never re-upload local advertising bytes")

        def attach_image(self, *, access_token: str, ad_id: str, image_hash: str) -> None:
            calls.append((ad_id, image_hash))

    attached, pending, failed = ad_goal_publication._attach_media(
        provider=Provider(),
        bundle=SimpleNamespace(access_token="token"),
        job=SimpleNamespace(
            id=asset.publication_job_id,
            business_id=asset.business_id,
        ),
        ad_id="ad-42",
    )

    assert (attached, pending, failed) == (True, False, False)
    assert calls == [("ad-42", "hash-1")]


def test_advertising_asset_application_has_no_persistent_storage_primitives() -> None:
    source = Path(assets.__file__).read_text(encoding="utf-8")
    assert "CLIENTPLATFORM_AD_ASSET_DIR" not in source
    assert "mkstemp" not in source
    assert "_write_asset" not in source
    assert "/var/lib/clientplatform/ad-assets" not in source
    assert "storage_path" not in source

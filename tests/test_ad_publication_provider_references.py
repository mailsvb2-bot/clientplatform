from __future__ import annotations

import hashlib
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from clientplatform.application import ad_goal_publication
from clientplatform.application import ad_provider_session
from clientplatform.application import ad_publication_assets as assets
from clientplatform.runtime import ad_media_monitor
from clientplatform.infrastructure.ad_publication_asset_repository import AdMediaUploadReservation
from clientplatform.integrations.yandex_direct import YandexDirectError, YandexTokenBundle
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


def test_image_bytes_claim_before_provider_and_persist_only_reference(monkeypatch) -> None:
    order: list[str] = []
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
            order.append("provider")
            captured["payload"] = payload
            return "provider-image-hash"

    class Repository:
        def __init__(self, _conn) -> None:
            pass

        def begin_upload(self, **kwargs):
            order.append("claim")
            captured["begin"] = kwargs
            return AdMediaUploadReservation("claimed", claim_token="claim-1")

        def complete_upload(self, **kwargs):
            order.append("complete")
            captured["complete"] = kwargs
            return AdPublicationAsset(
                publication_job_id=publication_job_id,
                business_id=actor.business_id,
                kind=AdPublicationAssetKind.IMAGE,
                source=AdPublicationAssetSource.UPLOAD,
                content_type="image/jpeg",
                original_name="owner-photo.png",
                sha256=str(captured["begin"]["sha256"]),
                size_bytes=int(captured["begin"]["size_bytes"]),
                provider_image_hash=str(kwargs["provider_image_hash"]),
                created_by_member_id=str(uuid4()),
                created_at="2026-09-29T00:00:00+00:00",
                updated_at="2026-09-29T00:00:00+00:00",
            )

    @contextmanager
    def fake_db():
        yield object()

    provider = Provider()
    monkeypatch.setattr(assets, "_publication_job", lambda **_kwargs: job)
    monkeypatch.setattr(assets, "get_db", fake_db)
    monkeypatch.setattr(assets, "AdPublicationAssetRepository", Repository)
    monkeypatch.setattr(
        assets,
        "load_bundle",
        lambda **_kwargs: (SimpleNamespace(), SimpleNamespace(access_token="token", refresh_token="")),
    )
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
        vault=object(),
    )

    assert order == ["claim", "provider", "complete"]
    uploaded = captured["payload"]
    assert isinstance(uploaded, bytes)
    assert uploaded.startswith(b"\xff\xd8\xff")
    assert result.provider_image_hash == "provider-image-hash"
    assert "storage_path" not in captured["begin"]



def test_video_bytes_claim_before_provider_and_persist_only_reference(monkeypatch) -> None:
    order: list[str] = []
    actor = SimpleNamespace(business_id=str(uuid4()))
    publication_job_id = str(uuid4())
    job = SimpleNamespace(
        id=publication_job_id,
        business_id=actor.business_id,
        connection_id=str(uuid4()),
    )

    class Provider:
        def upload_video(self, *, access_token: str, payload: bytes, name: str) -> str:
            order.append("provider")
            assert payload == b"video-bytes"
            assert name == "owner-video.mp4"
            return "provider-video-id"

    class Repository:
        def __init__(self, _conn) -> None:
            pass

        def begin_upload(self, **_kwargs):
            order.append("claim")
            return AdMediaUploadReservation("claimed", claim_token="video-claim")

        def complete_upload(self, **kwargs):
            order.append("complete")
            return AdPublicationAsset(
                publication_job_id=publication_job_id,
                business_id=actor.business_id,
                kind=AdPublicationAssetKind.VIDEO,
                source=AdPublicationAssetSource.UPLOAD,
                content_type="video/mp4",
                original_name="owner-video.mp4",
                sha256=hashlib.sha256(b"video-bytes").hexdigest(),
                size_bytes=len(b"video-bytes"),
                duration_seconds=10,
                provider_video_id=str(kwargs["provider_video_id"]),
                created_by_member_id=str(uuid4()),
                created_at="2026-09-29T00:00:00+00:00",
                updated_at="2026-09-29T00:00:00+00:00",
            )

    @contextmanager
    def fake_db():
        yield object()

    monkeypatch.setattr(assets, "_publication_job", lambda **_kwargs: job)
    monkeypatch.setattr(assets, "get_db", fake_db)
    monkeypatch.setattr(assets, "AdPublicationAssetRepository", Repository)
    monkeypatch.setattr(
        assets,
        "load_bundle",
        lambda **_kwargs: (
            SimpleNamespace(),
            SimpleNamespace(access_token="token", refresh_token=""),
        ),
    )
    monkeypatch.setattr(
        assets,
        "with_access_token",
        lambda *, operation, **_kwargs: operation("access-token"),
    )

    result = assets.attach_video_bytes(
        actor=actor,
        publication_job_id=publication_job_id,
        payload=b"video-bytes",
        content_type="video/mp4",
        original_name="owner-video.mp4",
        duration_seconds=10,
        provider=Provider(),
        vault=object(),
    )

    assert order == ["claim", "provider", "complete"]
    assert result.provider_video_id == "provider-video-id"


def test_ready_digest_reuses_provider_reference_without_second_upload(monkeypatch) -> None:
    actor = SimpleNamespace(business_id=str(uuid4()))
    publication_job_id = str(uuid4())
    normalized = assets._normalized_image(_jpeg())
    digest = hashlib.sha256(normalized).hexdigest()
    existing = AdPublicationAsset(
        publication_job_id=publication_job_id,
        business_id=actor.business_id,
        kind=AdPublicationAssetKind.IMAGE,
        source=AdPublicationAssetSource.UPLOAD,
        content_type="image/jpeg",
        original_name="image.jpg",
        sha256=digest,
        size_bytes=len(normalized),
        provider_image_hash="existing-hash",
        created_by_member_id=str(uuid4()),
        created_at="2026-09-29T00:00:00+00:00",
        updated_at="2026-09-29T00:00:00+00:00",
    )

    class Provider:
        def upload_image(self, **_kwargs):
            raise AssertionError("ready digest must not be uploaded twice")

    class Repository:
        def __init__(self, _conn) -> None:
            pass

        def begin_upload(self, **_kwargs):
            return AdMediaUploadReservation("ready", asset=existing)

    @contextmanager
    def fake_db():
        yield object()

    monkeypatch.setattr(
        assets,
        "_publication_job",
        lambda **_kwargs: SimpleNamespace(
            id=publication_job_id,
            business_id=actor.business_id,
            connection_id=str(uuid4()),
        ),
    )
    monkeypatch.setattr(assets, "get_db", fake_db)
    monkeypatch.setattr(assets, "AdPublicationAssetRepository", Repository)
    monkeypatch.setattr(
        assets,
        "load_bundle",
        lambda **_kwargs: (SimpleNamespace(), SimpleNamespace(access_token="token", refresh_token="")),
    )

    result = assets.attach_image_bytes(
        actor=actor,
        publication_job_id=publication_job_id,
        payload=_jpeg(),
        provider=Provider(),
        vault=object(),
    )
    assert result.provider_image_hash == "existing-hash"


def test_provider_failure_marks_claim_ambiguous_and_raises_recoverable_error(monkeypatch) -> None:
    actor = SimpleNamespace(business_id=str(uuid4()))
    publication_job_id = str(uuid4())
    marked: dict[str, object] = {}

    class Provider:
        def upload_image(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    class Repository:
        def __init__(self, _conn) -> None:
            pass

        def begin_upload(self, **_kwargs):
            return AdMediaUploadReservation("claimed", claim_token="claim-ambiguous")

        def mark_upload_ambiguous(self, **kwargs):
            marked.update(kwargs)

    @contextmanager
    def fake_db():
        yield object()

    monkeypatch.setattr(
        assets,
        "_publication_job",
        lambda **_kwargs: SimpleNamespace(
            id=publication_job_id,
            business_id=actor.business_id,
            connection_id=str(uuid4()),
        ),
    )
    monkeypatch.setattr(assets, "get_db", fake_db)
    monkeypatch.setattr(assets, "AdPublicationAssetRepository", Repository)
    monkeypatch.setattr(
        assets,
        "load_bundle",
        lambda **_kwargs: (
            SimpleNamespace(),
            SimpleNamespace(access_token="token", refresh_token=""),
        ),
    )
    monkeypatch.setattr(
        assets,
        "with_access_token",
        lambda *, operation, **_kwargs: operation("token"),
    )

    with pytest.raises(
        assets.AdPublicationAssetError,
        match="provider upload could not be confirmed",
    ):
        assets.attach_image_bytes(
            actor=actor,
            publication_job_id=publication_job_id,
            payload=_jpeg(),
            provider=Provider(),
            vault=object(),
        )

    assert marked["claim_token"] == "claim-ambiguous"
    assert marked["error_code"] == "ad_image_upload_ambiguous"


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


def test_yandex_provider_requires_enabled_and_configured(monkeypatch) -> None:
    monkeypatch.delenv("CLIENTPLATFORM_AD_CONNECTIONS_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="connections are disabled"):
        ad_provider_session.yandex_provider()

    monkeypatch.setenv("CLIENTPLATFORM_AD_CONNECTIONS_ENABLED", "1")
    monkeypatch.delenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID", raising=False)
    monkeypatch.delenv("CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI", raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        ad_provider_session.yandex_provider()

    monkeypatch.setenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID", "client-id")
    monkeypatch.setenv(
        "CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI",
        "https://example.test/oauth",
    )
    provider = ad_provider_session.yandex_provider()
    assert provider is not None


def test_with_access_token_refreshes_once_for_auth_error(monkeypatch) -> None:
    job = SimpleNamespace(business_id=str(uuid4()), connection_id=str(uuid4()))
    connection = SimpleNamespace()
    bundle = YandexTokenBundle(
        access_token="old-token",
        token_type="bearer",
        expires_in=3600,
        refresh_token="refresh-token",
        scope=(),
    )
    refreshed = YandexTokenBundle(
        access_token="new-token",
        token_type="bearer",
        expires_in=3600,
        refresh_token="refresh-token",
        scope=(),
    )
    seen: list[str] = []

    class Provider:
        pass

    def operation(token: str):
        seen.append(token)
        if token == "old-token":
            raise YandexDirectError("provider_http_401")
        return "ok"

    monkeypatch.setattr(
        ad_provider_session,
        "refresh_bundle",
        lambda **_kwargs: refreshed,
    )
    result = ad_provider_session.with_access_token(
        job=job,
        operation=operation,
        provider=Provider(),
        vault=object(),
        connection=connection,
        bundle=bundle,
    )

    assert result == "ok"
    assert seen == ["old-token", "new-token"]


def test_with_access_token_does_not_refresh_non_auth_or_missing_refresh_token(monkeypatch) -> None:
    job = SimpleNamespace(business_id=str(uuid4()), connection_id=str(uuid4()))
    connection = SimpleNamespace()
    provider = object()
    vault = object()

    for code, refresh_token in [
        ("provider_transport_unavailable", "refresh-token"),
        ("provider_http_401", None),
    ]:
        bundle = YandexTokenBundle(
            access_token="token",
            token_type="bearer",
            expires_in=3600,
            refresh_token=refresh_token,
            scope=(),
        )
        with pytest.raises(YandexDirectError):
            ad_provider_session.with_access_token(
                job=job,
                operation=lambda _token, error=code: (_ for _ in ()).throw(
                    YandexDirectError(error)
                ),
                provider=provider,
                vault=vault,
                connection=connection,
                bundle=bundle,
            )


@pytest.mark.asyncio
async def test_ad_media_monitor_start_is_idempotent_and_stop_cancels(monkeypatch) -> None:
    class FakeTask:
        def __init__(self) -> None:
            self.cancelled = False

        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            self.cancelled = True

        def __await__(self):
            async def _cancelled():
                raise asyncio.CancelledError
            return _cancelled().__await__()

    task = FakeTask()
    created: list[object] = []

    class Manager:
        def create(self, coro, *, name=None):
            created.append((coro, name))
            coro.close()
            return task

    monkeypatch.setattr(ad_media_monitor, "_task_manager", Manager())
    monkeypatch.setattr(ad_media_monitor, "_task", None)

    await ad_media_monitor.start_ad_media_monitor(object())
    await ad_media_monitor.start_ad_media_monitor(object())
    assert len(created) == 1
    assert created[0][1] == "clientplatform-ad-media-monitor"

    await ad_media_monitor.stop_ad_media_monitor(object())
    assert task.cancelled is True
    assert ad_media_monitor._task is None


@pytest.mark.asyncio
async def test_ad_media_monitor_loop_drains_batch_and_sleeps(monkeypatch) -> None:
    calls = 0
    sleeps: list[int] = []

    def process() -> bool:
        nonlocal calls
        calls += 1
        return calls < 3

    async def fake_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def fake_sleep(seconds: int):
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(ad_media_monitor, "process_one_pending_video_asset", process)
    monkeypatch.setattr(ad_media_monitor.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(ad_media_monitor.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(ad_media_monitor, "_interval_seconds", lambda: 45)

    with pytest.raises(asyncio.CancelledError):
        await ad_media_monitor._loop()

    assert calls == 3
    assert sleeps == [45]

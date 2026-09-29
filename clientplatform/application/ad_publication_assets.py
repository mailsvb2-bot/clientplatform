from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from clientplatform.application.ad_provider_session import (
    ad_vault,
    load_bundle,
    with_access_token,
    yandex_provider,
)
from clientplatform.domain.ad_publication_assets import (
    AdPublicationAsset,
    AdPublicationAssetError,
    AdPublicationAssetKind,
    AdPublicationAssetSource,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.ad_goal_publication_repository import (
    AdGoalPublicationRepository,
)
from clientplatform.infrastructure.ad_publication_asset_repository import (
    AdMediaUploadReservation,
    AdPublicationAssetRepository,
)
from clientplatform.infrastructure.ad_credential_vault import AdCredentialVault
from clientplatform.integrations.yandex_direct import YandexDirectError
from clientplatform.integrations.yandex_direct_media import MediaAwareYandexDirectProvider
from services.db import get_db, get_db_ro


_IMAGE_INPUT_LIMIT = 20_000_000
_IMAGE_OUTPUT_LIMIT = 10_000_000
_VIDEO_LIMIT = 100_000_000
_VIDEO_EXTENSIONS = frozenset({"mp4", "webm", "mov", "qt", "flv", "avi"})
_VIDEO_CONTENT_TYPES = frozenset(
    {
        "video/mp4",
        "video/webm",
        "video/quicktime",
        "video/x-flv",
        "video/x-msvideo",
        "application/octet-stream",
    }
)


def _normalized_image(payload: bytes) -> bytes:
    if not payload or len(payload) > _IMAGE_INPUT_LIMIT:
        raise AdPublicationAssetError("image is too large")
    try:
        from io import BytesIO

        with Image.open(BytesIO(payload)) as source:
            source.verify()
        with Image.open(BytesIO(payload)) as reopened:
            image = ImageOps.exif_transpose(reopened).convert("RGB")
            image = ImageOps.contain(image, (1080, 1080), method=Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (1080, 1080), "white")
            left = (1080 - image.width) // 2
            top = (1080 - image.height) // 2
            canvas.paste(image, (left, top))
            output = BytesIO()
            canvas.save(output, format="JPEG", quality=90, optimize=True)
            normalized = output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise AdPublicationAssetError("image format is unsupported or damaged") from exc
    if not normalized or len(normalized) > _IMAGE_OUTPUT_LIMIT:
        raise AdPublicationAssetError("image could not be normalized for advertising")
    return normalized


def _publication_job(*, actor: TenantContext, publication_job_id: str):
    with get_db_ro() as conn:
        return AdGoalPublicationRepository(conn).get(
            actor=actor,
            job_id=publication_job_id,
        )


def _reserve_upload(
    *,
    actor: TenantContext,
    publication_job_id: str,
    kind: AdPublicationAssetKind,
    source: AdPublicationAssetSource,
    content_type: str,
    original_name: str,
    sha256: str,
    size_bytes: int,
    duration_seconds: int | None,
) -> AdMediaUploadReservation:
    try:
        with get_db() as conn:
            return AdPublicationAssetRepository(conn).begin_upload(
                actor=actor,
                publication_job_id=publication_job_id,
                kind=kind,
                source=source,
                content_type=content_type,
                original_name=original_name,
                sha256=sha256,
                size_bytes=size_bytes,
                duration_seconds=duration_seconds,
            )
    except (sqlite3.Error, RuntimeError, ValueError) as exc:
        raise AdPublicationAssetError(
            "advertising media upload could not be reserved"
        ) from exc


def _reservation_result(
    reservation: AdMediaUploadReservation,
) -> AdPublicationAsset | None:
    if reservation.state == "ready" and reservation.asset is not None:
        return reservation.asset
    if reservation.state == "uploading":
        raise AdPublicationAssetError("advertising media upload is already in progress")
    if reservation.state == "ambiguous":
        raise AdPublicationAssetError(
            "advertising media upload result is ambiguous; automatic retry is blocked"
        )
    if reservation.state == "failed":
        raise AdPublicationAssetError("advertising media upload previously failed")
    if not reservation.claimed:
        raise AdPublicationAssetError("advertising media upload could not be claimed")
    return None


def _mark_upload_ambiguous(
    *,
    actor: TenantContext,
    publication_job_id: str,
    claim_token: str,
    error_code: str,
) -> None:
    try:
        with get_db() as conn:
            AdPublicationAssetRepository(conn).mark_upload_ambiguous(
                actor=actor,
                publication_job_id=publication_job_id,
                claim_token=claim_token,
                error_code=error_code,
            )
    except (sqlite3.Error, RuntimeError, ValueError):
        # The durable pre-claim remains in "uploading" and still blocks an
        # automatic second provider write.
        return


def _complete_upload(
    *,
    actor: TenantContext,
    publication_job_id: str,
    claim_token: str,
    provider_image_hash: str | None = None,
    provider_video_id: str | None = None,
) -> AdPublicationAsset:
    try:
        with get_db() as conn:
            return AdPublicationAssetRepository(conn).complete_upload(
                actor=actor,
                publication_job_id=publication_job_id,
                claim_token=claim_token,
                provider_image_hash=provider_image_hash,
                provider_video_id=provider_video_id,
            )
    except (sqlite3.Error, RuntimeError, ValueError) as exc:
        _mark_upload_ambiguous(
            actor=actor,
            publication_job_id=publication_job_id,
            claim_token=claim_token,
            error_code="ad_media_receipt_persist_ambiguous",
        )
        raise AdPublicationAssetError(
            "provider accepted media but the receipt could not be confirmed"
        ) from exc


def _provider_failure(
    *,
    actor: TenantContext,
    publication_job_id: str,
    reservation: AdMediaUploadReservation,
    code: str,
) -> AdPublicationAssetError:
    _mark_upload_ambiguous(
        actor=actor,
        publication_job_id=publication_job_id,
        claim_token=reservation.claim_token,
        error_code=code,
    )
    return AdPublicationAssetError(
        "advertising provider upload could not be confirmed"
    )


def attach_image_bytes(
    *,
    actor: TenantContext,
    publication_job_id: str,
    payload: bytes,
    source: AdPublicationAssetSource = AdPublicationAssetSource.UPLOAD,
    original_name: str = "image.jpg",
    provider: MediaAwareYandexDirectProvider | None = None,
    vault: AdCredentialVault | None = None,
) -> AdPublicationAsset:
    """Normalize in memory and persist only the provider-side image reference."""

    normalized = _normalized_image(payload)
    digest = hashlib.sha256(normalized).hexdigest()
    try:
        selected_provider = provider or yandex_provider()
        selected_vault = vault or ad_vault()
        job = _publication_job(actor=actor, publication_job_id=publication_job_id)
        connection, bundle = load_bundle(job=job, vault=selected_vault)
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise AdPublicationAssetError(
            "advertising provider authorization is unavailable"
        ) from exc
    except (RuntimeError, YandexDirectError) as exc:
        raise AdPublicationAssetError(
            "advertising provider authorization is unavailable"
        ) from exc
    reservation = _reserve_upload(
        actor=actor,
        publication_job_id=publication_job_id,
        kind=AdPublicationAssetKind.IMAGE,
        source=source,
        content_type="image/jpeg",
        original_name=original_name or "image.jpg",
        sha256=digest,
        size_bytes=len(normalized),
        duration_seconds=None,
    )
    existing = _reservation_result(reservation)
    if existing is not None:
        return existing
    try:
        image_hash = with_access_token(
            job=job,
            provider=selected_provider,
            vault=selected_vault,
            connection=connection,
            bundle=bundle,
            operation=lambda access_token: selected_provider.upload_image(
                access_token=access_token,
                payload=normalized,
                name=original_name or "image.jpg",
            ),
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        error = _provider_failure(
            actor=actor,
            publication_job_id=publication_job_id,
            reservation=reservation,
            code="ad_image_upload_ambiguous",
        )
        raise error from exc
    except (RuntimeError, YandexDirectError) as exc:
        error = _provider_failure(
            actor=actor,
            publication_job_id=publication_job_id,
            reservation=reservation,
            code="ad_image_upload_ambiguous",
        )
        raise error from exc
    return _complete_upload(
        actor=actor,
        publication_job_id=publication_job_id,
        claim_token=reservation.claim_token,
        provider_image_hash=image_hash,
    )


def attach_image_file(
    *,
    actor: TenantContext,
    publication_job_id: str,
    path: Path,
    source: AdPublicationAssetSource = AdPublicationAssetSource.GENERATED,
    provider: MediaAwareYandexDirectProvider | None = None,
    vault: AdCredentialVault | None = None,
) -> AdPublicationAsset:
    """Consume a transient image without promoting it to ClientPlatform storage."""

    candidate = path.expanduser().resolve(strict=True)
    if not candidate.is_file() or candidate.is_symlink():
        raise AdPublicationAssetError("generated image is unavailable")
    if candidate.stat().st_size > _IMAGE_INPUT_LIMIT:
        raise AdPublicationAssetError("generated image is too large")
    return attach_image_bytes(
        actor=actor,
        publication_job_id=publication_job_id,
        payload=candidate.read_bytes(),
        source=source,
        original_name=candidate.name,
        provider=provider,
        vault=vault,
    )


def attach_video_bytes(
    *,
    actor: TenantContext,
    publication_job_id: str,
    payload: bytes,
    content_type: str,
    original_name: str,
    duration_seconds: int,
    source: AdPublicationAssetSource = AdPublicationAssetSource.UPLOAD,
    provider: MediaAwareYandexDirectProvider | None = None,
    vault: AdCredentialVault | None = None,
) -> AdPublicationAsset:
    """Upload video at most once and persist only its provider-side identifier."""

    if not payload or len(payload) > _VIDEO_LIMIT:
        raise AdPublicationAssetError("video must be no larger than 100 MB")
    duration = int(duration_seconds)
    if duration < 5 or duration > 60:
        raise AdPublicationAssetError("video duration must be between 5 and 60 seconds")
    normalized_type = str(content_type or "application/octet-stream").strip().lower()
    if normalized_type not in _VIDEO_CONTENT_TYPES:
        raise AdPublicationAssetError("video format is unsupported")
    suffix = str(original_name or "video.mp4").rsplit(".", 1)[-1].lower()
    if suffix not in _VIDEO_EXTENSIONS:
        if normalized_type == "video/mp4":
            suffix = "mp4"
        elif normalized_type == "video/webm":
            suffix = "webm"
        elif normalized_type == "video/quicktime":
            suffix = "mov"
        else:
            raise AdPublicationAssetError("video file extension is unsupported")

    digest = hashlib.sha256(payload).hexdigest()
    try:
        selected_provider = provider or yandex_provider()
        selected_vault = vault or ad_vault()
        job = _publication_job(actor=actor, publication_job_id=publication_job_id)
        connection, bundle = load_bundle(job=job, vault=selected_vault)
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise AdPublicationAssetError(
            "advertising provider authorization is unavailable"
        ) from exc
    except (RuntimeError, YandexDirectError) as exc:
        raise AdPublicationAssetError(
            "advertising provider authorization is unavailable"
        ) from exc
    reservation = _reserve_upload(
        actor=actor,
        publication_job_id=publication_job_id,
        kind=AdPublicationAssetKind.VIDEO,
        source=source,
        content_type=normalized_type,
        original_name=original_name or f"video.{suffix}",
        sha256=digest,
        size_bytes=len(payload),
        duration_seconds=duration,
    )
    existing = _reservation_result(reservation)
    if existing is not None:
        return existing
    try:
        video_id = with_access_token(
            job=job,
            provider=selected_provider,
            vault=selected_vault,
            connection=connection,
            bundle=bundle,
            operation=lambda access_token: selected_provider.upload_video(
                access_token=access_token,
                payload=payload,
                name=original_name or f"video.{suffix}",
            ),
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        error = _provider_failure(
            actor=actor,
            publication_job_id=publication_job_id,
            reservation=reservation,
            code="ad_video_upload_ambiguous",
        )
        raise error from exc
    except (RuntimeError, YandexDirectError) as exc:
        error = _provider_failure(
            actor=actor,
            publication_job_id=publication_job_id,
            reservation=reservation,
            code="ad_video_upload_ambiguous",
        )
        raise error from exc
    return _complete_upload(
        actor=actor,
        publication_job_id=publication_job_id,
        claim_token=reservation.claim_token,
        provider_video_id=video_id,
    )


def list_reusable_images(
    *,
    actor: TenantContext,
    publication_job_id: str,
    limit: int = 8,
) -> list[AdPublicationAsset]:
    job = _publication_job(actor=actor, publication_job_id=publication_job_id)
    try:
        with get_db_ro() as conn:
            return AdPublicationAssetRepository(conn).list_reusable_images(
                actor=actor,
                connection_id=job.connection_id,
                limit=limit,
                exclude_publication_job_id=job.id,
            )
    except (sqlite3.Error, RuntimeError, ValueError) as exc:
        raise AdPublicationAssetError(
            "reusable advertising images are unavailable"
        ) from exc


def reuse_image_reference(
    *,
    actor: TenantContext,
    source_publication_job_id: str,
    target_publication_job_id: str,
) -> AdPublicationAsset:
    try:
        with get_db() as conn:
            return AdPublicationAssetRepository(conn).reuse_image_reference(
                actor=actor,
                source_publication_job_id=source_publication_job_id,
                target_publication_job_id=target_publication_job_id,
            )
    except (LookupError, sqlite3.Error, RuntimeError, ValueError) as exc:
        raise AdPublicationAssetError(
            "advertising image provider reference could not be reused"
        ) from exc


def remove_asset(*, actor: TenantContext, publication_job_id: str) -> bool:
    with get_db() as conn:
        return AdPublicationAssetRepository(conn).remove(
            actor=actor,
            publication_job_id=publication_job_id,
        )


def get_asset_for_worker(
    *,
    business_id: str,
    publication_job_id: str,
) -> AdPublicationAsset | None:
    with get_db_ro() as conn:
        return AdPublicationAssetRepository(conn).get_for_worker(
            business_id=business_id,
            publication_job_id=publication_job_id,
        )


def remember_provider_ids(
    *,
    business_id: str,
    publication_job_id: str,
    provider_image_hash: str | None = None,
    provider_video_id: str | None = None,
    provider_creative_id: str | None = None,
) -> AdPublicationAsset | None:
    with get_db() as conn:
        return AdPublicationAssetRepository(conn).remember_provider_ids(
            business_id=business_id,
            publication_job_id=publication_job_id,
            provider_image_hash=provider_image_hash,
            provider_video_id=provider_video_id,
            provider_creative_id=provider_creative_id,
        )


__all__ = [
    "attach_image_bytes",
    "attach_image_file",
    "attach_video_bytes",
    "get_asset_for_worker",
    "remember_provider_ids",
    "remove_asset",
]

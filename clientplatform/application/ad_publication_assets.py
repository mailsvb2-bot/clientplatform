from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from clientplatform.application.ad_provider_session import (
    ad_vault,
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
    AdPublicationAssetRepository,
)
from clientplatform.infrastructure.ad_credential_vault import AdCredentialVault
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
    """Normalize in memory, upload immediately, then persist only provider metadata."""

    normalized = _normalized_image(payload)
    digest = hashlib.sha256(normalized).hexdigest()
    selected_provider = provider or yandex_provider()
    selected_vault = vault or ad_vault()
    job = _publication_job(actor=actor, publication_job_id=publication_job_id)
    image_hash = with_access_token(
        job=job,
        provider=selected_provider,
        vault=selected_vault,
        operation=lambda access_token: selected_provider.upload_image(
            access_token=access_token,
            payload=normalized,
            name=original_name or "image.jpg",
        ),
    )
    with get_db() as conn:
        asset, _previous = AdPublicationAssetRepository(conn).replace(
            actor=actor,
            publication_job_id=publication_job_id,
            kind=AdPublicationAssetKind.IMAGE,
            source=source,
            storage_path="",
            content_type="image/jpeg",
            original_name=original_name or "image.jpg",
            sha256=digest,
            size_bytes=len(normalized),
            duration_seconds=None,
            provider_image_hash=image_hash,
        )
    return asset


def attach_image_file(
    *,
    actor: TenantContext,
    publication_job_id: str,
    path: Path,
    source: AdPublicationAssetSource = AdPublicationAssetSource.GENERATED,
    provider: MediaAwareYandexDirectProvider | None = None,
    vault: AdCredentialVault | None = None,
) -> AdPublicationAsset:
    """Consume one transient file without promoting it to durable ClientPlatform storage."""

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
    """Upload video bytes immediately; retain only Yandex video identifiers."""

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
    selected_provider = provider or yandex_provider()
    selected_vault = vault or ad_vault()
    job = _publication_job(actor=actor, publication_job_id=publication_job_id)
    video_id = with_access_token(
        job=job,
        provider=selected_provider,
        vault=selected_vault,
        operation=lambda access_token: selected_provider.upload_video(
            access_token=access_token,
            payload=payload,
            name=original_name or f"video.{suffix}",
        ),
    )
    with get_db() as conn:
        asset, _previous = AdPublicationAssetRepository(conn).replace(
            actor=actor,
            publication_job_id=publication_job_id,
            kind=AdPublicationAssetKind.VIDEO,
            source=source,
            storage_path="",
            content_type=normalized_type,
            original_name=original_name or f"video.{suffix}",
            sha256=digest,
            size_bytes=len(payload),
            duration_seconds=duration,
            provider_video_id=video_id,
        )
    return asset


def remove_asset(*, actor: TenantContext, publication_job_id: str) -> bool:
    with get_db() as conn:
        removed = AdPublicationAssetRepository(conn).remove(
            actor=actor,
            publication_job_id=publication_job_id,
        )
    return removed is not None


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


def read_asset_bytes(asset: AdPublicationAsset) -> bytes:
    del asset
    raise AdPublicationAssetError(
        "persistent advertising media bytes are forbidden; use provider references"
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


def cleanup_orphaned_assets(
    *,
    grace_seconds: int = 300,
    max_files: int = 200,
) -> int:
    """Compatibility no-op: advertising media no longer has local files."""

    del grace_seconds, max_files
    return 0


__all__ = [
    "attach_image_bytes",
    "attach_image_file",
    "attach_video_bytes",
    "cleanup_orphaned_assets",
    "get_asset_for_worker",
    "read_asset_bytes",
    "remember_provider_ids",
    "remove_asset",
]

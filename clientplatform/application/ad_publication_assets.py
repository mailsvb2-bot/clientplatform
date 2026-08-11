from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from clientplatform.domain.ad_publication_assets import (
    AdPublicationAsset,
    AdPublicationAssetError,
    AdPublicationAssetKind,
    AdPublicationAssetSource,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.ad_publication_asset_repository import (
    AdPublicationAssetRepository,
)
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


def _asset_root() -> Path:
    configured = str(
        os.getenv("CLIENTPLATFORM_AD_ASSET_DIR") or "/var/lib/clientplatform/ad-assets"
    ).strip()
    root = Path(configured).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink():
        raise AdPublicationAssetError("advertising asset storage is unavailable")
    return resolved


def _safe_unlink(path: str | None) -> None:
    if not path:
        return
    try:
        candidate = Path(path).resolve(strict=False)
        root = _asset_root()
        if candidate != root and root in candidate.parents and candidate.is_file():
            candidate.unlink(missing_ok=True)
    except OSError:
        return


def _write_asset(
    payload: bytes,
    *,
    business_id: str,
    publication_job_id: str,
    extension: str,
) -> tuple[str, str, int]:
    if not payload:
        raise AdPublicationAssetError("advertising asset is empty")
    digest = hashlib.sha256(payload).hexdigest()
    root = _asset_root()
    scope = hashlib.sha256(str(business_id).encode("ascii")).hexdigest()[:20]
    directory = root / scope / str(publication_job_id)
    directory.mkdir(parents=True, exist_ok=True)
    directory = directory.resolve(strict=True)
    if root not in directory.parents:
        raise AdPublicationAssetError("advertising asset path escaped storage root")
    suffix = "".join(ch for ch in extension.lower().lstrip(".") if ch.isalnum())[:8] or "bin"
    final = directory / f"{digest}.{suffix}"
    descriptor, temporary = tempfile.mkstemp(prefix=".incoming-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        Path(temporary).chmod(0o600)
        os.replace(temporary, final)
        final.chmod(0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return str(final), digest, len(payload)


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


def attach_image_bytes(
    *,
    actor: TenantContext,
    publication_job_id: str,
    payload: bytes,
    source: AdPublicationAssetSource = AdPublicationAssetSource.UPLOAD,
    original_name: str = "image.jpg",
) -> AdPublicationAsset:
    normalized = _normalized_image(payload)
    storage_path, digest, size = _write_asset(
        normalized,
        business_id=actor.business_id,
        publication_job_id=publication_job_id,
        extension="jpg",
    )
    try:
        with get_db() as conn:
            asset, previous = AdPublicationAssetRepository(conn).replace(
                actor=actor,
                publication_job_id=publication_job_id,
                kind=AdPublicationAssetKind.IMAGE,
                source=source,
                storage_path=storage_path,
                content_type="image/jpeg",
                original_name=original_name,
                sha256=digest,
                size_bytes=size,
                duration_seconds=None,
            )
    except Exception:  # validator: allow-wide-except - rollback local file on any DB boundary failure
        _safe_unlink(storage_path)
        raise
    if previous != storage_path:
        _safe_unlink(previous)
    return asset


def attach_image_file(
    *,
    actor: TenantContext,
    publication_job_id: str,
    path: Path,
    source: AdPublicationAssetSource = AdPublicationAssetSource.GENERATED,
) -> AdPublicationAsset:
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
) -> AdPublicationAsset:
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
    storage_path, digest, size = _write_asset(
        payload,
        business_id=actor.business_id,
        publication_job_id=publication_job_id,
        extension=suffix,
    )
    try:
        with get_db() as conn:
            asset, previous = AdPublicationAssetRepository(conn).replace(
                actor=actor,
                publication_job_id=publication_job_id,
                kind=AdPublicationAssetKind.VIDEO,
                source=source,
                storage_path=storage_path,
                content_type=normalized_type,
                original_name=original_name or f"video.{suffix}",
                sha256=digest,
                size_bytes=size,
                duration_seconds=duration,
            )
    except Exception:  # validator: allow-wide-except - rollback local file on any DB boundary failure
        _safe_unlink(storage_path)
        raise
    if previous != storage_path:
        _safe_unlink(previous)
    return asset


def remove_asset(*, actor: TenantContext, publication_job_id: str) -> bool:
    with get_db() as conn:
        path = AdPublicationAssetRepository(conn).remove(
            actor=actor,
            publication_job_id=publication_job_id,
        )
    _safe_unlink(path)
    return path is not None


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
    root = _asset_root()
    path = Path(asset.storage_path).resolve(strict=True)
    if root not in path.parents or not path.is_file() or path.is_symlink():
        raise AdPublicationAssetError("advertising asset storage reference is invalid")
    payload = path.read_bytes()
    if len(payload) != asset.size_bytes:
        raise AdPublicationAssetError("advertising asset size changed")
    if hashlib.sha256(payload).hexdigest() != asset.sha256:
        raise AdPublicationAssetError("advertising asset checksum changed")
    return payload


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
    "read_asset_bytes",
    "remember_provider_ids",
    "remove_asset",
]

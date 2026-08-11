from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from clientplatform.domain.tenancy import normalize_uuid


class AdPublicationAssetError(RuntimeError):
    """Base error for media attached to an advertising publication draft."""


class AdPublicationAssetKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class AdPublicationAssetSource(StrEnum):
    UPLOAD = "upload"
    GENERATED = "generated"


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class AdPublicationAsset:
    publication_job_id: str
    business_id: str
    kind: AdPublicationAssetKind
    source: AdPublicationAssetSource
    storage_path: str
    content_type: str
    original_name: str
    sha256: str
    size_bytes: int
    created_by_member_id: str
    created_at: str
    updated_at: str
    duration_seconds: int | None = None
    provider_image_hash: str | None = None
    provider_video_id: str | None = None
    provider_creative_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("publication_job_id", "business_id", "created_by_member_id"):
            object.__setattr__(
                self,
                name,
                normalize_uuid(getattr(self, name), field_name=name),
            )
        object.__setattr__(self, "kind", AdPublicationAssetKind(self.kind))
        object.__setattr__(self, "source", AdPublicationAssetSource(self.source))
        path = str(self.storage_path or "").strip()
        if not path or "\x00" in path or len(path) > 2048:
            raise ValueError("advertising asset storage path is invalid")
        object.__setattr__(self, "storage_path", path)
        content_type = str(self.content_type or "").strip().lower()
        if not content_type or len(content_type) > 120 or "\x00" in content_type:
            raise ValueError("advertising asset content type is invalid")
        object.__setattr__(self, "content_type", content_type)
        original_name = " ".join(str(self.original_name or "media").split())[:255]
        if not original_name:
            original_name = "media"
        object.__setattr__(self, "original_name", original_name)
        digest = str(self.sha256 or "").strip().lower()
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError("advertising asset SHA-256 is invalid")
        object.__setattr__(self, "sha256", digest)
        size = int(self.size_bytes)
        if size <= 0 or size > 100_000_000:
            raise ValueError("advertising asset size is invalid")
        object.__setattr__(self, "size_bytes", size)
        if self.duration_seconds is not None:
            duration = int(self.duration_seconds)
            if duration <= 0 or duration > 3600:
                raise ValueError("advertising asset duration is invalid")
            object.__setattr__(self, "duration_seconds", duration)
        for name in ("provider_image_hash", "provider_video_id", "provider_creative_id"):
            value = getattr(self, name)
            if value is not None:
                normalized = str(value).strip()
                if not normalized or len(normalized) > 255 or "\x00" in normalized:
                    raise ValueError(f"{name} is invalid")
                object.__setattr__(self, name, normalized)


__all__ = [
    "AdPublicationAsset",
    "AdPublicationAssetError",
    "AdPublicationAssetKind",
    "AdPublicationAssetSource",
]

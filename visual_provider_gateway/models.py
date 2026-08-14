from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import urllib.parse
from typing import Any, Literal

CreativeKind = Literal["image", "video"]
CreativeStatus = Literal["queued", "running", "succeeded", "failed"]


@dataclass(frozen=True)
class CreativeBrief:
    kind: CreativeKind
    prompt: str
    country_code: str = ""
    aspect_ratio: str = "1:1"
    duration_seconds: int = 5
    negative_prompt: str = ""
    reference_url: str = ""
    preferred_provider: str = ""
    brand_context: str = ""
    seed: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def normalized(self) -> "CreativeBrief":
        kind = str(self.kind or "").strip().lower()
        if kind not in {"image", "video"}:
            raise ValueError("creative kind must be image or video")
        prompt = str(self.prompt or "").strip()
        if not prompt:
            raise ValueError("creative prompt is required")
        ratio = _normalize_ratio(self.aspect_ratio)
        duration = max(2, min(int(self.duration_seconds or 5), 15))
        country = str(self.country_code or "").strip().upper()
        preferred = str(self.preferred_provider or "").strip().lower()
        return replace(
            self,
            kind=kind,  # type: ignore[arg-type]
            prompt=prompt,
            country_code=country,
            aspect_ratio=ratio,
            duration_seconds=duration,
            negative_prompt=str(self.negative_prompt or "").strip(),
            reference_url=str(self.reference_url or "").strip(),
            preferred_provider=preferred,
            brand_context=str(self.brand_context or "").strip(),
            metadata=dict(self.metadata or {}),
        )


@dataclass
class CreativeJob:
    provider: str
    kind: CreativeKind
    status: CreativeStatus
    external_id: str = ""
    model: str = ""
    mime_type: str = ""
    asset_path: str = ""
    media_url: str = ""
    error_code: str = ""
    provider_payload: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def done(self) -> bool:
        return self.status in {"succeeded", "failed"}

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "kind": self.kind,
            "status": self.status,
            "external_id": self.external_id,
            "model": self.model,
            "mime_type": self.mime_type,
            "asset_path": self.asset_path,
            "has_media_url": bool(self.media_url),
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str = ""
    api_key: str = ""
    model_image: str = ""
    model_video: str = ""
    folder_id: str = ""
    credentials: str = ""
    oauth_url: str = ""
    scope: str = ""
    ca_bundle_file: str = ""
    timeout_seconds: int = 30
    max_json_bytes: int = 4 * 1024 * 1024
    max_media_bytes: int = 256 * 1024 * 1024
    output_dir: str = ""

    def safe_dict(self) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(str(self.base_url or ""))
        safe_base = ""
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            port = f":{parsed.port}" if parsed.port else ""
            safe_base = f"{parsed.scheme}://{parsed.hostname}{port}"
        return {
            "name": self.name,
            "base_url": safe_base,
            "model_image": self.model_image,
            "model_video": self.model_video,
            "folder_id_configured": bool(self.folder_id),
            "credentials_configured": bool(self.credentials or self.api_key),
            "ca_bundle_configured": bool(self.ca_bundle_file),
            "timeout_seconds": self.timeout_seconds,
            "max_json_bytes": self.max_json_bytes,
            "max_media_bytes": self.max_media_bytes,
            "output_dir": self.output_dir,
        }


def _normalize_ratio(value: str) -> str:
    raw = str(value or "1:1").strip().lower().replace("x", ":")
    aliases = {
        "square": "1:1",
        "portrait": "9:16",
        "story": "9:16",
        "stories": "9:16",
        "landscape": "16:9",
        "wide": "16:9",
        "feed": "4:5",
    }
    raw = aliases.get(raw, raw)
    if ":" not in raw:
        return "1:1"
    left, right = raw.split(":", 1)
    try:
        width = max(1, int(left))
        height = max(1, int(right))
    except ValueError:
        return "1:1"
    return f"{width}:{height}"


def ensure_output_dir(path: str) -> Path:
    target = Path(path or "data/visual_creatives").expanduser()
    target.mkdir(parents=True, exist_ok=True)
    return target

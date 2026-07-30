from __future__ import annotations

from urllib.parse import urlsplit

PROGRAM_MEDIA_PREFIX = "program-media/"
VOICE_SUFFIX = ".ogg"


def is_voice_media_reference(reference: str) -> bool:
    parsed = urlsplit(str(reference or "").strip())
    key = parsed.path.lstrip("/")
    return (
        parsed.scheme == "s3"
        and key.startswith(PROGRAM_MEDIA_PREFIX)
        and key.lower().endswith(VOICE_SUFFIX)
        and not parsed.query
        and not parsed.fragment
    )


def mark_voice_media_reference(reference: str) -> str:
    normalized = str(reference or "").strip()
    if not is_voice_media_reference(normalized):
        raise ValueError("voice media reference must be a private OGG program object")
    return normalized


def unwrap_program_media_reference(reference: str) -> str:
    return str(reference or "").strip()


__all__ = [
    "PROGRAM_MEDIA_PREFIX",
    "VOICE_SUFFIX",
    "is_voice_media_reference",
    "mark_voice_media_reference",
    "unwrap_program_media_reference",
]

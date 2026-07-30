from __future__ import annotations

from urllib.parse import urlsplit

VOICE_PATH_SEGMENT = "voice"


def mark_voice_media_reference(reference: str) -> str:
    normalized = str(reference or "").strip()
    parsed = urlsplit(normalized)
    segments = parsed.path.lstrip("/").split("/")
    if parsed.scheme != "s3" or VOICE_PATH_SEGMENT not in segments:
        raise ValueError("voice media reference must use the private voice S3 scope")
    return normalized


def is_voice_media_reference(reference: str) -> bool:
    parsed = urlsplit(str(reference or "").strip())
    return (
        parsed.scheme == "s3"
        and VOICE_PATH_SEGMENT in parsed.path.lstrip("/").split("/")
        and not parsed.query
        and not parsed.fragment
    )


def unwrap_program_media_reference(reference: str) -> str:
    return str(reference or "").strip()


__all__ = [
    "VOICE_PATH_SEGMENT",
    "is_voice_media_reference",
    "mark_voice_media_reference",
    "unwrap_program_media_reference",
]

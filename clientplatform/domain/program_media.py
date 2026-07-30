from __future__ import annotations

from urllib.parse import urlsplit

VOICE_AUDIO_SCOPE = "audio"
VOICE_SUFFIX = ".ogg"


def is_voice_media_reference(reference: str) -> bool:
    parsed = urlsplit(str(reference or "").strip())
    segments = parsed.path.lstrip("/").split("/")
    return (
        parsed.scheme == "s3"
        and VOICE_AUDIO_SCOPE in segments
        and parsed.path.lower().endswith(VOICE_SUFFIX)
        and not parsed.query
        and not parsed.fragment
    )


def mark_voice_media_reference(reference: str) -> str:
    normalized = str(reference or "").strip()
    if not is_voice_media_reference(normalized):
        raise ValueError("voice media reference must be a private OGG audio object")
    return normalized


def unwrap_program_media_reference(reference: str) -> str:
    return str(reference or "").strip()


__all__ = [
    "VOICE_AUDIO_SCOPE",
    "VOICE_SUFFIX",
    "is_voice_media_reference",
    "mark_voice_media_reference",
    "unwrap_program_media_reference",
]

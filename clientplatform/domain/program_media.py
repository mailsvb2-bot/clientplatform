from __future__ import annotations

VOICE_S3_PREFIX = "voice+s3://"


def mark_voice_media_reference(reference: str) -> str:
    normalized = str(reference or "").strip()
    if not normalized.startswith("s3://"):
        raise ValueError("voice media reference must use private s3 storage")
    return f"voice+{normalized}"


def is_voice_media_reference(reference: str) -> bool:
    return str(reference or "").strip().startswith(VOICE_S3_PREFIX)


def unwrap_program_media_reference(reference: str) -> str:
    normalized = str(reference or "").strip()
    if is_voice_media_reference(normalized):
        return normalized.removeprefix("voice+")
    return normalized


__all__ = [
    "VOICE_S3_PREFIX",
    "is_voice_media_reference",
    "mark_voice_media_reference",
    "unwrap_program_media_reference",
]

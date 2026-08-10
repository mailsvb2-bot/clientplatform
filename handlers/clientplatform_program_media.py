from __future__ import annotations

"""Turn control-bot media into bot-independent private lesson references."""

import asyncio
import logging
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clientplatform.application.program_media import (
    ProgramMediaIngestPolicy,
    ProgramMediaStoreError,
    program_media_ingest_policy,
    store_program_media,
)
from clientplatform.domain.program_media import mark_voice_media_reference
from clientplatform.domain.programs import ContentKind, normalize_content_ref

if TYPE_CHECKING:
    from aiogram.types import Message
else:
    Message = Any

_EXTENSION_RE = re.compile(r"^[a-z0-9]{1,10}$")
StoreMedia = Callable[..., Any]
log = logging.getLogger(__name__)


class _UnavailableTelegramBadRequest(Exception):
    pass


class _UnavailableTelegramNetworkError(Exception):
    pass


def _telegram_error_types() -> tuple[type[BaseException], type[BaseException]]:
    try:
        from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
    except ImportError:
        return _UnavailableTelegramBadRequest, _UnavailableTelegramNetworkError
    return TelegramBadRequest, TelegramNetworkError


class ProgramMediaIngestError(RuntimeError):
    """Sanitized ingest failure that never contains Telegram or S3 secrets."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        normalized = str(code or "program_media_ingest_failure").strip()[:120]
        super().__init__(normalized)
        self.code = normalized
        self.retryable = bool(retryable)


@dataclass(frozen=True, slots=True)
class TelegramMediaInput:
    content_kind: ContentKind
    file_id: str
    reported_size: int | None
    content_type: str
    extension: str
    voice: bool = False


def _safe_extension(value: str | None, fallback: str) -> str:
    raw = str(value or "").strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    suffix = raw.rsplit(".", 1)[-1].lower() if "." in raw else ""
    if _EXTENSION_RE.fullmatch(suffix):
        return suffix
    return fallback


def _reported_size(value: Any) -> int | None:
    raw = getattr(value, "file_size", None)
    if raw is None:
        return None
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


def _select_media(message: Message) -> TelegramMediaInput | None:
    if message.audio is not None:
        return TelegramMediaInput(
            content_kind=ContentKind.AUDIO,
            file_id=str(message.audio.file_id),
            reported_size=_reported_size(message.audio),
            content_type=str(message.audio.mime_type or "audio/mpeg"),
            extension=_safe_extension(message.audio.file_name, "mp3"),
        )
    if message.voice is not None:
        return TelegramMediaInput(
            content_kind=ContentKind.AUDIO,
            file_id=str(message.voice.file_id),
            reported_size=_reported_size(message.voice),
            content_type=str(message.voice.mime_type or "audio/ogg"),
            extension="ogg",
            voice=True,
        )
    if message.video is not None:
        return TelegramMediaInput(
            content_kind=ContentKind.VIDEO,
            file_id=str(message.video.file_id),
            reported_size=_reported_size(message.video),
            content_type=str(message.video.mime_type or "video/mp4"),
            extension=_safe_extension(message.video.file_name, "mp4"),
        )
    if message.document is not None:
        return TelegramMediaInput(
            content_kind=ContentKind.DOCUMENT,
            file_id=str(message.document.file_id),
            reported_size=_reported_size(message.document),
            content_type=str(message.document.mime_type or "application/octet-stream"),
            extension=_safe_extension(message.document.file_name, "bin"),
        )
    if message.photo:
        photo = message.photo[-1]
        return TelegramMediaInput(
            content_kind=ContentKind.IMAGE,
            file_id=str(photo.file_id),
            reported_size=_reported_size(photo),
            content_type="image/jpeg",
            extension="jpg",
        )
    return None


def _new_private_tempfile(extension: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix="clientplatform-program-media-",
        suffix=f".{extension}",
    )
    os.close(descriptor)
    path = Path(raw_path)
    path.chmod(0o600)
    return path


async def materialize_program_content(
    message: Message,
    *,
    business_id: str,
    policy: ProgramMediaIngestPolicy | None = None,
    store_media: StoreMedia = store_program_media,
) -> tuple[ContentKind, str]:
    text = str(message.text or "").strip()
    media = _select_media(message)
    if media is None:
        if text:
            return ContentKind.TEXT, normalize_content_ref(text)
        raise ValueError(
            "поддерживаются аудио, видео, документ, изображение или текст"
        )

    selected_policy = policy or program_media_ingest_policy()
    if not selected_policy.enabled:
        raise ProgramMediaIngestError("program_media_ingest_disabled")
    if (
        media.reported_size is not None
        and media.reported_size > selected_policy.max_bytes
    ):
        raise ProgramMediaIngestError("program_media_too_large")

    telegram_bad_request, telegram_network_error = _telegram_error_types()
    temporary = _new_private_tempfile(media.extension)
    try:
        try:
            telegram_file = await message.bot.get_file(media.file_id)
            file_path = str(getattr(telegram_file, "file_path", "") or "").strip()
            remote_size = _reported_size(telegram_file)
            if not file_path:
                raise ProgramMediaIngestError("program_media_telegram_path_missing")
            if remote_size is not None and remote_size > selected_policy.max_bytes:
                raise ProgramMediaIngestError("program_media_too_large")
            await message.bot.download_file(
                file_path,
                destination=temporary,
                timeout=selected_policy.timeout_seconds,
            )
        except (telegram_network_error, asyncio.TimeoutError):
            raise ProgramMediaIngestError(
                "program_media_telegram_transport_failure",
                retryable=True,
            ) from None
        except telegram_bad_request:
            raise ProgramMediaIngestError(
                "program_media_telegram_file_unavailable"
            ) from None

        try:
            downloaded_size = temporary.stat().st_size
        except OSError:
            raise ProgramMediaIngestError("program_media_download_missing") from None
        if downloaded_size <= 0:
            raise ProgramMediaIngestError("program_media_download_empty")
        if downloaded_size > selected_policy.max_bytes:
            raise ProgramMediaIngestError("program_media_too_large")
        if media.reported_size not in {None, 0} and downloaded_size != media.reported_size:
            raise ProgramMediaIngestError("program_media_download_size_mismatch")

        try:
            stored = await asyncio.to_thread(
                store_media,
                temporary,
                business_id=business_id,
                content_kind=media.content_kind,
                content_type=media.content_type,
                extension=media.extension,
            )
        except ProgramMediaStoreError as exc:
            raise ProgramMediaIngestError(
                exc.code,
                retryable=exc.retryable,
            ) from None
        reference = normalize_content_ref(stored.reference)
        if media.voice:
            reference = mark_voice_media_reference(reference)
        return media.content_kind, reference
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            log.warning("Failed to remove temporary program media file", exc_info=True)


__all__ = [
    "ProgramMediaIngestError",
    "TelegramMediaInput",
    "materialize_program_content",
]

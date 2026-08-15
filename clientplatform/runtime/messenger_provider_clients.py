from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from clientplatform.domain.programs import ContentKind
from runtime.messenger_max_sender import MaxBotSender
from runtime.messenger_vk_sender import VkBotSender


_MAX_MEDIA_BYTES = 20_000_000


def _provider_message_id(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value).strip()
    if isinstance(value, dict):
        for key in ("message_id", "mid", "id"):
            if value.get(key) is not None:
                resolved = _provider_message_id(value.get(key))
                if resolved:
                    return resolved
        for key in ("message", "body", "response"):
            if value.get(key) is not None:
                resolved = _provider_message_id(value.get(key))
                if resolved:
                    return resolved
    return ""


def _vk_random_id(idempotency_key: str) -> int:
    digest = hashlib.blake2s(
        str(idempotency_key or "").encode("utf-8"),
        digest_size=4,
    ).digest()
    value = int.from_bytes(digest, "big") & 0x7FFFFFFF
    return value or 1


def _safe_suffix(reference: str) -> str:
    suffix = Path(urlsplit(reference).path).suffix.lower()
    if not suffix or len(suffix) > 10 or any(char not in ".abcdefghijklmnopqrstuvwxyz0123456789" for char in suffix):
        return ".bin"
    return suffix


def _materialize_media_sync(reference: str) -> tuple[Path, bool]:
    raw = str(reference or "").strip()
    if not raw:
        raise ValueError("media reference must not be empty")
    if raw.startswith("https://"):
        request = Request(
            raw,
            headers={"User-Agent": "ClientPlatform/1.0", "Accept": "*/*"},
            method="GET",
        )
        tmp = tempfile.NamedTemporaryFile(
            prefix="clientplatform-provider-media-",
            suffix=_safe_suffix(raw),
            delete=False,
        )
        path = Path(tmp.name)
        size = 0
        try:
            with tmp:
                with urlopen(request, timeout=30.0) as response:  # nosec B310 - HTTPS checked below
                    final_url = str(response.geturl() or "")
                    if not final_url.startswith("https://"):
                        raise ValueError("media redirect must remain HTTPS")
                    declared = response.headers.get("Content-Length")
                    if declared is not None and int(declared) > _MAX_MEDIA_BYTES:
                        raise ValueError("provider media exceeds size limit")
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > _MAX_MEDIA_BYTES:
                            raise ValueError("provider media exceeds size limit")
                        tmp.write(chunk)
            if size <= 0:
                raise ValueError("provider media download is empty")
            return path, True
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    if "://" in raw:
        raise ValueError("provider media reference must be HTTPS or a local resolved path")
    path = Path(raw)
    if not path.is_file():
        raise ValueError("resolved provider media file does not exist")
    if path.stat().st_size <= 0 or path.stat().st_size > _MAX_MEDIA_BYTES:
        raise ValueError("resolved provider media file has invalid size")
    return path, False


async def _materialize_media(reference: str) -> tuple[Path, bool]:
    return await asyncio.to_thread(_materialize_media_sync, reference)


class VkRuntimeClient:
    """Canonical VK client backed by the already hardened provider sender."""

    async def send_text(
        self,
        *,
        token: str,
        external_subject: str,
        text: str,
        idempotency_key: str,
    ) -> str:
        result = await VkBotSender(token=token).send_text(
            external_subject,
            text,
            random_id=_vk_random_id(idempotency_key),
        )
        message_id = _provider_message_id(result)
        if not message_id:
            raise ValueError("VK provider response has no message id")
        return message_id

    async def send_media(
        self,
        *,
        token: str,
        external_subject: str,
        kind: ContentKind,
        media: str,
        idempotency_key: str,
    ) -> str:
        path, temporary = await _materialize_media(media)
        sender = VkBotSender(token=token)
        kwargs = {"random_id": _vk_random_id(idempotency_key)}
        try:
            if kind == ContentKind.IMAGE:
                result = await sender.send_image_file(external_subject, path, **kwargs)
            elif kind == ContentKind.AUDIO:
                result = await sender.send_audio_file(external_subject, path, **kwargs)
            else:
                # VK has no canonical native-video method in the retained sender;
                # video is delivered honestly as a document instead of fabricated parity.
                result = await sender.send_document_file(external_subject, path, **kwargs)
        finally:
            if temporary:
                path.unlink(missing_ok=True)
        message_id = _provider_message_id(result)
        if not message_id:
            raise ValueError("VK provider response has no message id")
        return message_id


class MaxRuntimeClient:
    """Canonical MAX client backed by the official HTTPS-only provider sender."""

    async def send_text(
        self,
        *,
        token: str,
        external_subject: str,
        text: str,
        idempotency_key: str,
    ) -> str:
        del idempotency_key
        result = await MaxBotSender(token=token).send_text(external_subject, text)
        message_id = _provider_message_id(result)
        if not message_id:
            raise ValueError("MAX provider response has no message id")
        return message_id

    async def send_media(
        self,
        *,
        token: str,
        external_subject: str,
        kind: ContentKind,
        media: str,
        idempotency_key: str,
    ) -> str:
        del idempotency_key
        path, temporary = await _materialize_media(media)
        sender = MaxBotSender(token=token)
        try:
            if kind == ContentKind.IMAGE:
                result = await sender.send_image_file(external_subject, path)
            elif kind == ContentKind.AUDIO:
                result = await sender.send_audio_file(external_subject, path)
            else:
                # Current MAX retained sender exposes file, image and audio. Video
                # is therefore sent as a file until native-video capability exists.
                result = await sender.send_document_file(external_subject, path)
        finally:
            if temporary:
                path.unlink(missing_ok=True)
        message_id = _provider_message_id(result)
        if not message_id:
            raise ValueError("MAX provider response has no message id")
        return message_id

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
import tempfile
import urllib.error
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from clientplatform.domain.programs import ContentKind
from services.messenger.provider_transport import (
    ProviderUploadURLRejected,
    validate_provider_upload_url,
)


_MAX_MEDIA_BYTES = 20_000_000
_MAX_MEDIA_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        del req, fp, code, msg, headers, newurl
        return None


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


def _validate_public_media_url(url: str) -> str:
    try:
        parsed = validate_provider_upload_url(url)
    except ProviderUploadURLRejected as exc:
        raise ValueError(f"provider media URL rejected: {exc.code}") from exc
    host = str(parsed.hostname or "").strip()
    if not host:
        raise ValueError("provider media URL host is required")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                host,
                parsed.port or 443,
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise ValueError("provider media DNS resolution failed") from exc
    if not addresses:
        raise ValueError("provider media DNS resolution returned no addresses")
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError("provider media URL resolved to an invalid address") from exc
        if not parsed_address.is_global:
            raise ValueError("provider media URL resolves to a non-public address")
    return str(url).strip()


def _open_public_media_url(raw_url: str):  # noqa: ANN201
    current = str(raw_url or "").strip()
    opener = build_opener(_NoRedirectHandler())
    for redirect_count in range(_MAX_MEDIA_REDIRECTS + 1):
        _validate_public_media_url(current)
        request = Request(
            current,
            headers={"User-Agent": "ClientPlatform/1.0", "Accept": "*/*"},
            method="GET",
        )
        try:
            response = opener.open(request, timeout=30.0)
        except urllib.error.HTTPError as exc:
            if int(exc.code) not in _REDIRECT_STATUSES:
                raise
            location = str(exc.headers.get("Location") or "").strip()
            exc.close()
            if not location:
                raise ValueError("provider media redirect has no Location") from None
            if redirect_count >= _MAX_MEDIA_REDIRECTS:
                raise ValueError("provider media redirect limit exceeded") from None
            current = urljoin(current, location)
            continue
        observed = str(response.geturl() or "").strip()
        if observed != current:
            response.close()
            raise ValueError("provider media transport followed an unvalidated redirect")
        return response
    raise ValueError("provider media redirect limit exceeded")


def _materialize_media_sync(reference: str) -> tuple[Path, bool]:
    raw = str(reference or "").strip()
    if not raw:
        raise ValueError("media reference must not be empty")
    if raw.startswith("https://"):
        tmp = tempfile.NamedTemporaryFile(
            prefix="clientplatform-provider-media-",
            suffix=_safe_suffix(raw),
            delete=False,
        )
        path = Path(tmp.name)
        size = 0
        materialized = False
        try:
            with tmp:
                with _open_public_media_url(raw) as response:
                    declared = response.headers.get("Content-Length")
                    if declared is not None:
                        try:
                            declared_size = int(declared)
                        except (TypeError, ValueError) as exc:
                            raise ValueError("provider media Content-Length is invalid") from exc
                        if declared_size > _MAX_MEDIA_BYTES:
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
            materialized = True
        finally:
            if not materialized:
                path.unlink(missing_ok=True)
        return path, True
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


def _vk_sender(token: str):  # noqa: ANN202
    # Retained provider transport is imported only at the actual provider
    # boundary. Importing canonical ClientPlatform runtime must never pull
    # Metrotherapy presentation dependencies into dependency-light domains.
    from runtime.messenger_vk_sender import VkBotSender

    return VkBotSender(token=token)


def _max_sender(token: str):  # noqa: ANN202
    from runtime.messenger_max_sender import MaxBotSender

    return MaxBotSender(token=token)


class VkRuntimeClient:
    """Canonical VK client backed by the retained low-level provider transport."""

    async def send_text(
        self,
        *,
        token: str,
        external_subject: str,
        text: str,
        idempotency_key: str,
    ) -> str:
        result = await _vk_sender(token).send_text(
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
        sender = _vk_sender(token)
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
    """Canonical MAX client backed by the official HTTPS-only provider transport."""

    async def send_text(
        self,
        *,
        token: str,
        external_subject: str,
        text: str,
        idempotency_key: str,
    ) -> str:
        del idempotency_key
        result = await _max_sender(token).send_text(
            external_subject,
            text,
            legacy_ui=False,
        )
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
        sender = _max_sender(token)
        try:
            if kind == ContentKind.IMAGE:
                result = await sender.send_image_file(external_subject, path)
            elif kind == ContentKind.AUDIO:
                result = await sender.send_audio_file(external_subject, path)
            elif kind == ContentKind.VIDEO:
                result = await sender.send_video_file(external_subject, path)
            else:
                result = await sender.send_document_file(external_subject, path)
        finally:
            if temporary:
                path.unlink(missing_ok=True)
        message_id = _provider_message_id(result)
        if not message_id:
            raise ValueError("MAX provider response has no message id")
        return message_id

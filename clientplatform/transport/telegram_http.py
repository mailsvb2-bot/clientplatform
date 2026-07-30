from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit


JsonPayload = Mapping[str, str]
PostJson = Callable[[str, JsonPayload, float], Awaitable[tuple[int, Any]]]
PostMultipart = Callable[
    [str, str, str, str, float, int],
    Awaitable[tuple[int, Any]],
]

_EXTENSION_RE = re.compile(r"^[a-z0-9]{1,10}$")
_DEFAULT_MULTIPART_MAX_BYTES = 20_000_000


class TelegramBotApiError(RuntimeError):
    """Sanitized Telegram Bot API failure safe for persistence."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        normalized = str(code or "telegram_bot_api_error").strip()[:160]
        super().__init__(normalized)
        self.code = normalized
        self.retryable = bool(retryable)


def _media_reference(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise TelegramBotApiError("telegram_media_reference_empty", retryable=False)
    if "://" in normalized and not normalized.startswith(("https://", "http://")):
        raise TelegramBotApiError(
            "telegram_media_reference_unresolved",
            retryable=False,
        )
    return normalized


def _response_message_id(status: int, body: Any) -> str:
    if not isinstance(body, dict):
        raise TelegramBotApiError(
            "telegram_response_invalid",
            retryable=status >= 500 or status == 429,
        )
    ok = bool(body.get("ok"))
    if status < 200 or status >= 300 or not ok:
        raw_code = body.get("error_code")
        try:
            error_code = int(raw_code)
        except (TypeError, ValueError):
            error_code = status
        retryable = error_code == 429 or error_code >= 500
        raise TelegramBotApiError(
            f"telegram_api_{error_code or 'failure'}",
            retryable=retryable,
        )
    result = body.get("result")
    if not isinstance(result, dict):
        raise TelegramBotApiError("telegram_result_invalid", retryable=False)
    message_id = result.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, (int, str)):
        raise TelegramBotApiError("telegram_message_id_missing", retryable=False)
    normalized_message_id = str(message_id).strip()
    if not normalized_message_id:
        raise TelegramBotApiError("telegram_message_id_missing", retryable=False)
    return normalized_message_id


async def _aiohttp_post_json(
    url: str,
    payload: JsonPayload,
    timeout_seconds: float,
) -> tuple[int, Any]:
    try:
        import aiohttp
    except ImportError:
        raise TelegramBotApiError(
            "telegram_http_dependency_missing",
            retryable=False,
        ) from None

    timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                json=dict(payload),
                allow_redirects=False,
            ) as response:
                status = int(response.status)
                try:
                    body = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    body = None
                return status, body
    except asyncio.TimeoutError:
        raise TelegramBotApiError(
            "telegram_http_timeout",
            retryable=True,
        ) from None
    except aiohttp.ClientError:
        raise TelegramBotApiError(
            "telegram_http_transport_failure",
            retryable=True,
        ) from None


def _multipart_filename(media_url: str) -> str:
    suffix = PurePosixPath(urlsplit(media_url).path).suffix.lower().lstrip(".")
    extension = suffix if _EXTENSION_RE.fullmatch(suffix) else "bin"
    return f"clientplatform-media.{extension}"


async def _aiohttp_post_multipart(
    url: str,
    field_name: str,
    chat_id: str,
    media_url: str,
    timeout_seconds: float,
    max_bytes: int,
) -> tuple[int, Any]:
    try:
        import aiohttp
    except ImportError:
        raise TelegramBotApiError(
            "telegram_http_dependency_missing",
            retryable=False,
        ) from None

    timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(media_url, allow_redirects=False) as source:
                source_status = int(source.status)
                if source_status != 200:
                    raise TelegramBotApiError(
                        "telegram_media_gateway_fetch_failed",
                        retryable=source_status == 429 or source_status >= 500,
                    )
                raw_length = source.headers.get("Content-Length")
                if raw_length:
                    try:
                        declared_length = int(raw_length)
                    except ValueError:
                        raise TelegramBotApiError(
                            "telegram_media_gateway_length_invalid",
                            retryable=False,
                        ) from None
                    if declared_length <= 0 or declared_length > max_bytes:
                        raise TelegramBotApiError(
                            "telegram_media_gateway_size_invalid",
                            retryable=False,
                        )
                content_type = str(
                    source.headers.get("Content-Type") or "application/octet-stream"
                ).split(";", 1)[0].strip()
                if not content_type or "\r" in content_type or "\n" in content_type:
                    content_type = "application/octet-stream"

                with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as body:
                    total = 0
                    async for chunk in source.content.iter_chunked(256 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise TelegramBotApiError(
                                "telegram_media_gateway_size_invalid",
                                retryable=False,
                            )
                        body.write(chunk)
                    if total <= 0:
                        raise TelegramBotApiError(
                            "telegram_media_gateway_empty",
                            retryable=False,
                        )
                    body.seek(0)
                    form = aiohttp.FormData()
                    form.add_field("chat_id", str(chat_id))
                    form.add_field(
                        field_name,
                        body,
                        filename=_multipart_filename(media_url),
                        content_type=content_type,
                    )
                    async with session.post(
                        url,
                        data=form,
                        allow_redirects=False,
                    ) as response:
                        status = int(response.status)
                        try:
                            response_body = await response.json(content_type=None)
                        except (aiohttp.ContentTypeError, ValueError):
                            response_body = None
                        return status, response_body
    except TelegramBotApiError:
        raise
    except asyncio.TimeoutError:
        raise TelegramBotApiError(
            "telegram_http_timeout",
            retryable=True,
        ) from None
    except aiohttp.ClientError:
        raise TelegramBotApiError(
            "telegram_http_transport_failure",
            retryable=True,
        ) from None


class AiohttpTelegramBotClient:
    """Minimal Bot API client used by the ClientPlatform dispatch adapter.

    Private gateway objects are downloaded into a bounded spooled file and
    uploaded as multipart data through the selected business bot.  Arbitrary
    public HTTPS references keep the normal Telegram URL-send behavior.
    """

    def __init__(
        self,
        *,
        base_url: str = "https://api.telegram.org",
        timeout_seconds: float = 20.0,
        post_json: PostJson | None = None,
        post_multipart: PostMultipart | None = None,
        multipart_media_base_url: str = "",
        multipart_max_bytes: int = _DEFAULT_MULTIPART_MAX_BYTES,
    ) -> None:
        normalized_base = str(base_url or "").strip().rstrip("/")
        if not normalized_base.startswith("https://"):
            raise ValueError("Telegram Bot API base URL must use HTTPS")
        timeout = float(timeout_seconds)
        if timeout <= 0 or timeout > 120:
            raise ValueError("Telegram Bot API timeout must be between 0 and 120 seconds")
        max_bytes = int(multipart_max_bytes)
        if max_bytes <= 0 or max_bytes > _DEFAULT_MULTIPART_MAX_BYTES:
            raise ValueError("Telegram multipart media limit must be between 1 and 20000000")

        gateway = str(multipart_media_base_url or "").strip().rstrip("/")
        if gateway:
            parsed = urlsplit(gateway)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("Telegram multipart media base URL must use HTTPS")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("Telegram multipart media base URL is invalid")
            self._media_gateway_origin = f"{parsed.scheme}://{parsed.netloc}"
            self._media_gateway_path = parsed.path.rstrip("/")
        else:
            self._media_gateway_origin = ""
            self._media_gateway_path = ""

        self._base_url = normalized_base
        self._timeout_seconds = timeout
        self._post_json = post_json or _aiohttp_post_json
        self._post_multipart = post_multipart or _aiohttp_post_multipart
        self._multipart_max_bytes = max_bytes

    def _bot_url(self, *, token: str, method: str) -> str:
        normalized_token = str(token or "").strip()
        if not normalized_token:
            raise TelegramBotApiError("telegram_credential_empty", retryable=False)
        safe_token = quote(normalized_token, safe=":_-.")
        return f"{self._base_url}/bot{safe_token}/{method}"

    def _is_private_gateway_media(self, reference: str) -> bool:
        if not self._media_gateway_origin:
            return False
        parsed = urlsplit(reference)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        media_prefix = f"{self._media_gateway_path}/media/"
        return (
            origin == self._media_gateway_origin
            and parsed.path.startswith(media_prefix)
            and not parsed.fragment
        )

    async def _call(
        self,
        *,
        token: str,
        method: str,
        payload: JsonPayload,
    ) -> str:
        status, body = await self._post_json(
            self._bot_url(token=token, method=method),
            payload,
            self._timeout_seconds,
        )
        return _response_message_id(status, body)

    async def _call_media(
        self,
        *,
        token: str,
        method: str,
        field_name: str,
        chat_id: str,
        reference: str,
    ) -> str:
        selected = _media_reference(reference)
        if self._is_private_gateway_media(selected):
            status, body = await self._post_multipart(
                self._bot_url(token=token, method=method),
                field_name,
                str(chat_id),
                selected,
                self._timeout_seconds,
                self._multipart_max_bytes,
            )
            return _response_message_id(status, body)
        return await self._call(
            token=token,
            method=method,
            payload={"chat_id": str(chat_id), field_name: selected},
        )

    async def send_message(self, *, token: str, chat_id: str, text: str) -> str:
        return await self._call(
            token=token,
            method="sendMessage",
            payload={"chat_id": str(chat_id), "text": str(text)},
        )

    async def send_audio(self, *, token: str, chat_id: str, audio: str) -> str:
        return await self._call_media(
            token=token,
            method="sendAudio",
            field_name="audio",
            chat_id=chat_id,
            reference=audio,
        )

    async def send_video(self, *, token: str, chat_id: str, video: str) -> str:
        return await self._call_media(
            token=token,
            method="sendVideo",
            field_name="video",
            chat_id=chat_id,
            reference=video,
        )

    async def send_document(self, *, token: str, chat_id: str, document: str) -> str:
        return await self._call_media(
            token=token,
            method="sendDocument",
            field_name="document",
            chat_id=chat_id,
            reference=document,
        )

    async def send_photo(self, *, token: str, chat_id: str, photo: str) -> str:
        return await self._call_media(
            token=token,
            method="sendPhoto",
            field_name="photo",
            chat_id=chat_id,
            reference=photo,
        )


__all__ = [
    "AiohttpTelegramBotClient",
    "PostJson",
    "PostMultipart",
    "TelegramBotApiError",
]

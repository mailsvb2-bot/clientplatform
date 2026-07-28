from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote


JsonPayload = Mapping[str, str]
PostJson = Callable[[str, JsonPayload, float], Awaitable[tuple[int, Any]]]


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


class AiohttpTelegramBotClient:
    """Minimal Bot API client used by the clientplatform dispatch adapter.

    The token exists only while constructing the official Bot API request URL.
    No raw response URL or provider description is propagated to logs or DB.
    """

    def __init__(
        self,
        *,
        base_url: str = "https://api.telegram.org",
        timeout_seconds: float = 20.0,
        post_json: PostJson | None = None,
    ) -> None:
        normalized_base = str(base_url or "").strip().rstrip("/")
        if not normalized_base.startswith("https://"):
            raise ValueError("Telegram Bot API base URL must use HTTPS")
        timeout = float(timeout_seconds)
        if timeout <= 0 or timeout > 120:
            raise ValueError("Telegram Bot API timeout must be between 0 and 120 seconds")
        self._base_url = normalized_base
        self._timeout_seconds = timeout
        self._post_json = post_json or _aiohttp_post_json

    async def _call(
        self,
        *,
        token: str,
        method: str,
        payload: JsonPayload,
    ) -> str:
        normalized_token = str(token or "").strip()
        if not normalized_token:
            raise TelegramBotApiError("telegram_credential_empty", retryable=False)
        safe_token = quote(normalized_token, safe=":_-.")
        url = f"{self._base_url}/bot{safe_token}/{method}"
        status, body = await self._post_json(url, payload, self._timeout_seconds)

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

    async def send_message(self, *, token: str, chat_id: str, text: str) -> str:
        return await self._call(
            token=token,
            method="sendMessage",
            payload={"chat_id": str(chat_id), "text": str(text)},
        )

    async def send_audio(self, *, token: str, chat_id: str, audio: str) -> str:
        return await self._call(
            token=token,
            method="sendAudio",
            payload={"chat_id": str(chat_id), "audio": _media_reference(audio)},
        )

    async def send_video(self, *, token: str, chat_id: str, video: str) -> str:
        return await self._call(
            token=token,
            method="sendVideo",
            payload={"chat_id": str(chat_id), "video": _media_reference(video)},
        )

    async def send_document(self, *, token: str, chat_id: str, document: str) -> str:
        return await self._call(
            token=token,
            method="sendDocument",
            payload={"chat_id": str(chat_id), "document": _media_reference(document)},
        )

    async def send_photo(self, *, token: str, chat_id: str, photo: str) -> str:
        return await self._call(
            token=token,
            method="sendPhoto",
            payload={"chat_id": str(chat_id), "photo": _media_reference(photo)},
        )

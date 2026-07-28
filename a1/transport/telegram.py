from __future__ import annotations

from typing import Protocol

from a1.domain.connections import ClaimedDispatch, ConnectionPlatform
from a1.domain.programs import ContentKind


class TelegramBotClient(Protocol):
    """Minimal Telegram sending surface injected into A1.

    The client owns HTTP/Bot API details. The adapter only selects the correct
    operation and never stores the credential.
    """

    async def send_message(
        self,
        *,
        token: str,
        chat_id: str,
        text: str,
    ) -> str: ...

    async def send_audio(
        self,
        *,
        token: str,
        chat_id: str,
        audio: str,
    ) -> str: ...

    async def send_video(
        self,
        *,
        token: str,
        chat_id: str,
        video: str,
    ) -> str: ...

    async def send_document(
        self,
        *,
        token: str,
        chat_id: str,
        document: str,
    ) -> str: ...

    async def send_photo(
        self,
        *,
        token: str,
        chat_id: str,
        photo: str,
    ) -> str: ...


class TelegramDispatchAdapter:
    platform = ConnectionPlatform.TELEGRAM

    def __init__(self, client: TelegramBotClient):
        self._client = client

    async def send(self, item: ClaimedDispatch, credential: str) -> str:
        token = str(credential or "").strip()
        if not token:
            raise ValueError("resolved Telegram credential must not be empty")

        chat_id = item.external_subject
        payload = item.dispatch.payload_ref
        kind = item.dispatch.payload_kind

        if kind == ContentKind.AUDIO:
            result = await self._client.send_audio(
                token=token,
                chat_id=chat_id,
                audio=payload,
            )
        elif kind == ContentKind.VIDEO:
            result = await self._client.send_video(
                token=token,
                chat_id=chat_id,
                video=payload,
            )
        elif kind == ContentKind.DOCUMENT:
            result = await self._client.send_document(
                token=token,
                chat_id=chat_id,
                document=payload,
            )
        elif kind == ContentKind.IMAGE:
            result = await self._client.send_photo(
                token=token,
                chat_id=chat_id,
                photo=payload,
            )
        elif kind in {
            ContentKind.TEXT,
            ContentKind.LINK,
            ContentKind.TASK,
            ContentKind.MIXED,
        }:
            result = await self._client.send_message(
                token=token,
                chat_id=chat_id,
                text=payload,
            )
        else:  # pragma: no cover - enum exhaustiveness guard
            raise ValueError(f"unsupported Telegram content kind: {kind.value}")

        provider_message_id = str(result or "").strip()
        if not provider_message_id:
            raise ValueError("Telegram sender returned an empty message id")
        return provider_message_id

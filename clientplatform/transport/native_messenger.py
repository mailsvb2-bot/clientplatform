from __future__ import annotations

from typing import Protocol

from clientplatform.domain.connections import ClaimedDispatch, ConnectionPlatform
from clientplatform.domain.customer_interactions import CustomerInteractionMessage
from clientplatform.domain.programs import ContentKind
from clientplatform.transport.media import MediaReferenceResolver


class NativeMessengerClient(Protocol):
    """Provider client used by VK/MAX adapters without leaking provider logic inward."""

    async def send_text(
        self,
        *,
        token: str,
        external_subject: str,
        text: str,
        idempotency_key: str,
    ) -> str: ...

    async def send_media(
        self,
        *,
        token: str,
        external_subject: str,
        kind: ContentKind,
        media: str,
        idempotency_key: str,
    ) -> str: ...

    async def send_interaction(
        self,
        *,
        token: str,
        external_subject: str,
        interaction: CustomerInteractionMessage,
        idempotency_key: str,
    ) -> str: ...


_MEDIA_KINDS = frozenset(
    {
        ContentKind.AUDIO,
        ContentKind.VIDEO,
        ContentKind.DOCUMENT,
        ContentKind.IMAGE,
    }
)
_TEXT_KINDS = frozenset(
    {
        ContentKind.TEXT,
        ContentKind.LINK,
        ContentKind.TASK,
        ContentKind.MIXED,
    }
)


class _NativeMessengerDispatchAdapter:
    platform: ConnectionPlatform

    def __init__(
        self,
        client: NativeMessengerClient,
        *,
        media_resolver: MediaReferenceResolver | None = None,
    ) -> None:
        self._client = client
        self._media_resolver = media_resolver

    async def send(self, item: ClaimedDispatch, credential: str) -> str:
        token = str(credential or "").strip()
        if not token:
            raise ValueError(f"resolved {self.platform.value} credential must not be empty")
        if item.dispatch.platform != self.platform:
            raise ValueError("dispatch platform does not match adapter")

        kind = item.dispatch.payload_kind
        payload = item.dispatch.payload_ref
        if (
            str(getattr(item.dispatch, "source_kind", "")) == "customer_interaction"
            and kind == ContentKind.MIXED
        ):
            interaction = CustomerInteractionMessage.from_json(payload)
            result = await self._client.send_interaction(
                token=token,
                external_subject=item.external_subject,
                interaction=interaction,
                idempotency_key=item.dispatch.idempotency_key,
            )
        elif kind in _MEDIA_KINDS:
            if self._media_resolver is not None:
                payload = await self._media_resolver.resolve(payload, kind)
            result = await self._client.send_media(
                token=token,
                external_subject=item.external_subject,
                kind=kind,
                media=payload,
                idempotency_key=item.dispatch.idempotency_key,
            )
        elif kind in _TEXT_KINDS:
            result = await self._client.send_text(
                token=token,
                external_subject=item.external_subject,
                text=payload,
                idempotency_key=item.dispatch.idempotency_key,
            )
        else:  # pragma: no cover - enum exhaustiveness guard
            raise ValueError(f"unsupported {self.platform.value} content kind: {kind.value}")

        provider_message_id = str(result or "").strip()
        if not provider_message_id:
            raise ValueError(f"{self.platform.value} sender returned an empty message id")
        return provider_message_id


class VkDispatchAdapter(_NativeMessengerDispatchAdapter):
    platform = ConnectionPlatform.VK


class MaxDispatchAdapter(_NativeMessengerDispatchAdapter):
    platform = ConnectionPlatform.MAX

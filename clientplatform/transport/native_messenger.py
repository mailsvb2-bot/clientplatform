from __future__ import annotations

import json
from typing import Mapping, Protocol

from clientplatform.domain.connections import ClaimedDispatch, ConnectionPlatform
from clientplatform.domain.customer_interactions import CustomerInteractionMessage
from clientplatform.domain.programs import ContentKind
from clientplatform.transport.media import MediaReferenceResolver


_RUNTIME_LINKS_KEY = "_runtime_link_buttons"


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
        button_links: Mapping[str, str] | None = None,
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
_INTERACTION_SOURCE_KINDS = frozenset(
    {
        "customer_interaction",
        "member_interaction",
    }
)


def _runtime_button_links(
    payload: str,
    interaction: CustomerInteractionMessage,
) -> dict[str, str]:
    try:
        raw = json.loads(str(payload or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("native interaction runtime payload is invalid") from exc
    if not isinstance(raw, dict):
        raise ValueError("native interaction runtime payload is invalid")
    value = raw.get(_RUNTIME_LINKS_KEY)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("native interaction runtime links are invalid")

    allowed_commands = {
        button.command
        for row in interaction.rows
        for button in row
    }
    links: dict[str, str] = {}
    for command, raw_url in value.items():
        normalized_command = str(command or "").strip()
        url = str(raw_url or "").strip()
        if normalized_command not in allowed_commands:
            raise ValueError("native interaction runtime link command is not in payload")
        if (
            not url.startswith("https://")
            or len(url) > 2048
            or any(ord(char) < 32 or ord(char) == 127 for char in url)
        ):
            raise ValueError("native interaction runtime link URL is invalid")
        links[normalized_command] = url
    return links


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
            str(getattr(item.dispatch, "source_kind", ""))
            in _INTERACTION_SOURCE_KINDS
            and kind == ContentKind.MIXED
        ):
            interaction = CustomerInteractionMessage.from_json(payload)
            button_links = _runtime_button_links(payload, interaction)
            kwargs = {
                "token": token,
                "external_subject": item.external_subject,
                "interaction": interaction,
                "idempotency_key": item.dispatch.idempotency_key,
            }
            if button_links:
                # Existing injected test/legacy clients keep the historical
                # signature for ordinary interactions. The extra argument is
                # passed only when the worker materialized an ephemeral link.
                kwargs["button_links"] = button_links
            result = await self._client.send_interaction(**kwargs)
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
            raise ValueError(
                f"unsupported {self.platform.value} content kind: {kind.value}"
            )

        provider_message_id = str(result or "").strip()
        if not provider_message_id:
            raise ValueError(
                f"{self.platform.value} sender returned an empty message id"
            )
        return provider_message_id


class VkDispatchAdapter(_NativeMessengerDispatchAdapter):
    platform = ConnectionPlatform.VK


class MaxDispatchAdapter(_NativeMessengerDispatchAdapter):
    platform = ConnectionPlatform.MAX

from __future__ import annotations

import unittest
from uuid import uuid4

from clientplatform.domain.connections import (
    ConnectionPlatform,
    DispatchStatus,
)
from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from clientplatform.domain.programs import ContentKind
from clientplatform.infrastructure.unified_dispatch_outbox import (
    ClaimedProviderDispatch,
    ProviderDispatch,
)
from clientplatform.transport.native_messenger import (
    MaxDispatchAdapter,
    VkDispatchAdapter,
)


class _InteractionClient:
    def __init__(self) -> None:
        self.interactions: list[CustomerInteractionMessage] = []
        self.texts: list[str] = []

    async def send_interaction(
        self,
        *,
        token: str,
        external_subject: str,
        interaction: CustomerInteractionMessage,
        idempotency_key: str,
    ) -> str:
        assert token == "provider-token"
        assert external_subject == "700001"
        assert idempotency_key == "member-interaction:test"
        self.interactions.append(interaction)
        return "provider-message-1"

    async def send_text(self, *, token: str, external_subject: str, text: str, idempotency_key: str) -> str:
        del token, external_subject, idempotency_key
        self.texts.append(text)
        return "unexpected-text"

    async def send_media(self, **kwargs) -> str:
        del kwargs
        raise AssertionError("member UI must not enter media delivery")


def _claimed(platform: ConnectionPlatform) -> ClaimedProviderDispatch:
    interaction = CustomerInteractionMessage(
        text="ClientPlatform · рабочий кабинет",
        rows=((CustomerInteractionButton(label="👥 Клиенты", command="cpm:customers"),),),
    )
    stamp = "2026-08-21T05:00:00+00:00"
    return ClaimedProviderDispatch(
        dispatch=ProviderDispatch(
            id=str(uuid4()),
            business_id=str(uuid4()),
            platform=platform,
            source_kind="member_interaction",
            source_id="member:101:test",
            connection_id=str(uuid4()),
            external_subject="700001",
            payload_kind=ContentKind.MIXED,
            payload_ref=interaction.to_json(),
            idempotency_key="member-interaction:test",
            status=DispatchStatus.SENDING,
            attempts=0,
            available_at=stamp,
            created_at=stamp,
            updated_at=stamp,
            locked_at=stamp,
            lock_token="lease-1",
        ),
        external_subject="700001",
        credential_reference="secret://member/test",
    )


class NativeMemberTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_vk_member_interaction_uses_native_interaction_not_json_text(self) -> None:
        client = _InteractionClient()
        message_id = await VkDispatchAdapter(client).send(
            _claimed(ConnectionPlatform.VK),
            "provider-token",
        )
        self.assertEqual("provider-message-1", message_id)
        self.assertEqual([], client.texts)
        self.assertEqual("cpm:customers", client.interactions[0].rows[0][0].command)

    async def test_max_member_interaction_uses_native_interaction_not_json_text(self) -> None:
        client = _InteractionClient()
        message_id = await MaxDispatchAdapter(client).send(
            _claimed(ConnectionPlatform.MAX),
            "provider-token",
        )
        self.assertEqual("provider-message-1", message_id)
        self.assertEqual([], client.texts)
        self.assertEqual("ClientPlatform · рабочий кабинет", client.interactions[0].text)

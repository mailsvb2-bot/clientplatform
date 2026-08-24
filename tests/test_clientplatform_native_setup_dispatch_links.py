from __future__ import annotations

import json
import unittest
from contextlib import nullcontext
from dataclasses import replace
from unittest import mock
from uuid import uuid4

from clientplatform.application import dispatch_worker
from clientplatform.domain.connections import ConnectionPlatform, DispatchStatus
from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from clientplatform.domain.programs import ContentKind
from clientplatform.infrastructure.unified_dispatch_outbox import (
    ClaimedProviderDispatch,
    ProviderDispatch,
)
from clientplatform.runtime.messenger_provider_clients import MaxRuntimeClient, VkRuntimeClient
from clientplatform.transport.base import AdapterRegistry


_SETUP_SESSION = "00000000-0000-4000-8000-000000000099"
_SETUP_COMMAND = f"cpm:setup:{_SETUP_SESSION}"
_SETUP_URL = "https://client.example.test/clientplatform/connect/BEARER_VALUE"


def _interaction() -> CustomerInteractionMessage:
    return CustomerInteractionMessage(
        text="Подключение мессенджера",
        rows=(
            (
                CustomerInteractionButton(label="Подключить", command=_SETUP_COMMAND),
                CustomerInteractionButton(label="Назад", command="cpm:menu"),
            ),
        ),
    )


def _claimed(platform: ConnectionPlatform) -> ClaimedProviderDispatch:
    stamp = "2026-08-21T08:00:00+00:00"
    dispatch = ProviderDispatch(
        id=str(uuid4()),
        business_id=str(uuid4()),
        platform=platform,
        source_kind="member_interaction",
        source_id=str(uuid4()),
        connection_id=str(uuid4()),
        external_subject="700001",
        payload_kind=ContentKind.MIXED,
        payload_ref=_interaction().to_json(),
        idempotency_key=f"member-interaction:{uuid4()}",
        status=DispatchStatus.SENDING,
        attempts=0,
        available_at=stamp,
        created_at=stamp,
        updated_at=stamp,
        locked_at=stamp,
        lock_token="lease-token",
    )
    return ClaimedProviderDispatch(
        dispatch=dispatch,
        external_subject=dispatch.external_subject,
        credential_reference="secret://env/CLIENTPLATFORM_SECRET_NATIVE_TEST",
    )


class _Credentials:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, _reference: str) -> str:
        self.calls += 1
        return "provider-secret"


class _Adapter:
    def __init__(self, platform: ConnectionPlatform) -> None:
        self.platform = platform
        self.calls = 0
        self.item: ClaimedProviderDispatch | None = None

    async def send(self, item: ClaimedProviderDispatch, _credential: str) -> str:
        self.calls += 1
        self.item = item
        return "provider-message-1"


class _Repository:
    def __init__(self, claimed: ClaimedProviderDispatch) -> None:
        self.claimed = claimed
        self.boundary_calls = 0
        self.sent_calls = 0
        self.reschedule_calls = 0
        self.last_error = ""

    def claim_due(self, **_kwargs: object) -> list[ClaimedProviderDispatch]:
        return [self.claimed]

    def native_interaction_claim_can_cross_provider_boundary(self, _item: object) -> bool:
        return True

    def mark_provider_non_replay_boundary(self, _item: object, **_kwargs: object) -> bool:
        self.boundary_calls += 1
        return self.claimed.dispatch.platform == ConnectionPlatform.MAX

    def mark_sent(self, _item: object, **_kwargs: object) -> ProviderDispatch:
        self.sent_calls += 1
        return replace(self.claimed.dispatch, status=DispatchStatus.SENT)

    def reschedule(self, _item: object, *, error: str, **_kwargs: object) -> ProviderDispatch:
        self.reschedule_calls += 1
        self.last_error = error
        return replace(self.claimed.dispatch, status=DispatchStatus.DEAD)


class NativeSetupDispatchBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_materializes_setup_url_only_in_transient_send_item(self) -> None:
        claimed = _claimed(ConnectionPlatform.VK)
        repository = _Repository(claimed)
        credentials = _Credentials()
        adapter = _Adapter(ConnectionPlatform.VK)

        with (
            mock.patch.object(dispatch_worker, "get_db", lambda: nullcontext(object())),
            mock.patch.object(
                dispatch_worker,
                "DispatchOutboxRepository",
                lambda _conn: repository,
            ),
        ):
            result = await dispatch_worker.run_dispatch_batch(
                credential_provider=credentials,
                adapters=AdapterRegistry([adapter]),
                limit=1,
                interaction_link_resolver=lambda **_kwargs: _SETUP_URL,
            )

        self.assertEqual((result.claimed, result.sent, result.dead), (1, 1, 0))
        self.assertEqual(credentials.calls, 1)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(repository.sent_calls, 1)
        self.assertNotIn(_SETUP_URL, claimed.dispatch.payload_ref)
        self.assertNotIn("BEARER_VALUE", claimed.dispatch.payload_ref)
        self.assertIsNotNone(adapter.item)
        runtime_payload = json.loads(adapter.item.dispatch.payload_ref)  # type: ignore[union-attr]
        self.assertEqual(runtime_payload["_runtime_link_buttons"], {_SETUP_COMMAND: _SETUP_URL})

    async def test_unresolvable_max_setup_link_dies_before_secret_and_non_replay_boundary(self) -> None:
        claimed = _claimed(ConnectionPlatform.MAX)
        repository = _Repository(claimed)
        credentials = _Credentials()
        adapter = _Adapter(ConnectionPlatform.MAX)

        def _reject(**_kwargs: object) -> str:
            raise RuntimeError("sensitive internal reason with BEARER_VALUE")

        with (
            mock.patch.object(dispatch_worker, "get_db", lambda: nullcontext(object())),
            mock.patch.object(
                dispatch_worker,
                "DispatchOutboxRepository",
                lambda _conn: repository,
            ),
        ):
            result = await dispatch_worker.run_dispatch_batch(
                credential_provider=credentials,
                adapters=AdapterRegistry([adapter]),
                limit=1,
                max_attempts=8,
                interaction_link_resolver=_reject,
            )

        self.assertEqual((result.claimed, result.sent, result.retried, result.dead), (1, 0, 0, 1))
        self.assertEqual(credentials.calls, 0)
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(repository.boundary_calls, 0)
        self.assertEqual(repository.reschedule_calls, 1)
        self.assertNotIn("sensitive internal reason", repository.last_error)
        self.assertNotIn("BEARER_VALUE", repository.last_error)


class _VkSender:
    def __init__(self) -> None:
        self.keyboard: dict[str, object] | None = None

    async def send_text(self, _subject: str, _text: str, **kwargs: object) -> dict[str, int]:
        self.keyboard = json.loads(str(kwargs["keyboard_json"]))
        return {"message_id": 101}


class _MaxSender:
    def __init__(self) -> None:
        self.attachments: list[dict[str, object]] | None = None

    async def send_text(self, _subject: str, _text: str, **kwargs: object) -> dict[str, str]:
        self.attachments = kwargs.get("attachments")  # type: ignore[assignment]
        return {"message_id": "max-101"}


class NativeSetupProviderButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_vk_setup_button_is_open_link_while_regular_button_stays_text(self) -> None:
        sender = _VkSender()
        with mock.patch(
            "clientplatform.runtime.messenger_provider_clients._vk_sender",
            return_value=sender,
        ):
            await VkRuntimeClient().send_interaction(
                token="provider-token",
                external_subject="700001",
                interaction=_interaction(),
                idempotency_key="vk:test:setup",
                button_links={_SETUP_COMMAND: _SETUP_URL},
            )

        self.assertIsNotNone(sender.keyboard)
        buttons = sender.keyboard["buttons"][0]  # type: ignore[index]
        setup_button = buttons[0]
        regular_button = buttons[1]
        self.assertEqual(
            setup_button,
            {
                "action": {
                    "type": "open_link",
                    "link": _SETUP_URL,
                    "label": "Подключить",
                }
            },
        )
        self.assertEqual(regular_button["action"]["type"], "text")
        self.assertEqual(regular_button["color"], "secondary")
        self.assertEqual(
            json.loads(regular_button["action"]["payload"]),
            {"command": "cpm:menu"},
        )

    async def test_max_setup_button_is_link_while_regular_button_stays_message(self) -> None:
        sender = _MaxSender()
        with mock.patch(
            "clientplatform.runtime.messenger_provider_clients._max_sender",
            return_value=sender,
        ):
            await MaxRuntimeClient().send_interaction(
                token="provider-token",
                external_subject="700001",
                interaction=_interaction(),
                idempotency_key="max:test:setup",
                button_links={_SETUP_COMMAND: _SETUP_URL},
            )

        self.assertIsNotNone(sender.attachments)
        buttons = sender.attachments[0]["payload"]["buttons"][0]  # type: ignore[index]
        self.assertEqual(
            buttons[0],
            {"type": "link", "text": "Подключить", "url": _SETUP_URL},
        )
        self.assertEqual(
            buttons[1],
            {
                "type": "message",
                "text": "Назад",
                "payload": {"command": "cpm:menu"},
            },
        )


if __name__ == "__main__":
    unittest.main()

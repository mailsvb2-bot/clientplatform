from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.messenger_channels import MessengerIngressRoute
from clientplatform.runtime.messenger_provider_clients import MaxRuntimeClient
from runtime.messenger_max_sender import MAX_API2_BASE_URL, MaxBotSender
from runtime.messenger_payloads import (
    max_event_key,
    max_raw_message,
    vk_event_key,
    vk_raw_message,
)

_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None


class _FakeRequest:
    def __init__(self, payload: dict[str, object], *, route_id: str, headers: dict[str, str] | None = None) -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self.match_info = {"route_id": route_id}
        self.headers = headers or {}

    async def read(self) -> bytes:
        return self._raw


@unittest.skipUnless(_AIOHTTP_AVAILABLE, "aiohttp runtime dependency is not installed in dependency-light Canon")
class OmnichannelRuntimeIngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_max_callback_is_acknowledged_with_route_token(self) -> None:
        from clientplatform.runtime.messenger_channel_ingress import (
            _ack_max_message_callback,
        )

        route = SimpleNamespace(
            id=str(uuid4()),
            business_id=str(uuid4()),
            connection_id=str(uuid4()),
        )
        sender = SimpleNamespace(answer_callback=AsyncMock())
        credential_provider = SimpleNamespace(resolve=lambda reference: "max-token")
        with (
            patch(
                "clientplatform.runtime.messenger_channel_ingress._connection_credential_reference",
                return_value="vault://max-token",
            ) as credential_reference,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.MaxBotSender",
                return_value=sender,
            ) as sender_factory,
        ):
            await _ack_max_message_callback(
                {
                    "update_type": "message_callback",
                    "callback": {"callback_id": "callback-81001"},
                },
                route=route,
                credential_provider=credential_provider,
            )

        credential_reference.assert_called_once_with(route, ConnectionPlatform.MAX)
        sender_factory.assert_called_once_with(token="max-token")
        sender.answer_callback.assert_awaited_once_with(
            callback_id="callback-81001"
        )

    async def test_vk_confirmation_returns_route_scoped_secret_before_customer_extraction(self) -> None:
        from clientplatform.runtime.messenger_channel_ingress import canonical_vk_webhook

        route = MessengerIngressRoute(
            id=str(uuid4()),
            business_id=str(uuid4()),
            connection_id=str(uuid4()),
            platform=ConnectionPlatform.VK,
            external_route_id="424242",
            webhook_secret_reference="secret://env/CLIENTPLATFORM_SECRET_VK_WEBHOOK_TEST",
            confirmation_code_reference="secret://env/CLIENTPLATFORM_SECRET_VK_CONFIRMATION_TEST",
            status="active",
            created_by_member_id=str(uuid4()),
            created_at="2026-08-16T00:00:00+00:00",
            updated_at="2026-08-16T00:00:00+00:00",
        )
        request = _FakeRequest(
            {
                "type": "confirmation",
                "group_id": "424242",
                "secret": "route-webhook-secret",
            },
            route_id=route.id,
        )

        def _resolve_secret(reference: str) -> str:
            if reference == route.webhook_secret_reference:
                return "route-webhook-secret"
            if reference == route.confirmation_code_reference:
                return "vk-confirmation-code"
            raise AssertionError(f"unexpected secret reference: {reference}")

        with (
            patch(
                "clientplatform.runtime.messenger_channel_ingress.resolve_messenger_ingress_route",
                return_value=route,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.EnvironmentCredentialProvider.resolve",
                side_effect=_resolve_secret,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress._vk_raw_message",
                side_effect=AssertionError("confirmation must not enter customer extraction"),
            ),
        ):
            response = await canonical_vk_webhook(request)  # type: ignore[arg-type]

        self.assertEqual(200, response.status)
        self.assertEqual("vk-confirmation-code", response.text)

    async def test_accepted_vk_command_refreshes_latest_channel_activity(self) -> None:
        from clientplatform.runtime.messenger_channel_ingress import canonical_vk_webhook

        route = MessengerIngressRoute(
            id=str(uuid4()),
            business_id=str(uuid4()),
            connection_id=str(uuid4()),
            platform=ConnectionPlatform.VK,
            external_route_id="424242",
            webhook_secret_reference="secret://env/CLIENTPLATFORM_SECRET_VK_WEBHOOK_TEST",
            confirmation_code_reference="secret://env/CLIENTPLATFORM_SECRET_VK_CONFIRMATION_TEST",
            status="active",
            created_by_member_id=str(uuid4()),
            created_at="2026-08-16T00:00:00+00:00",
            updated_at="2026-08-16T00:00:00+00:00",
        )
        request = _FakeRequest(
            {
                "type": "message_new",
                "event_id": "vk-event-1",
                "group_id": "424242",
                "secret": "route-webhook-secret",
            },
            route_id=route.id,
        )
        identity = SimpleNamespace(
            customer_id=str(uuid4()),
            external_subject="778899",
        )

        with (
            patch(
                "clientplatform.runtime.messenger_channel_ingress.resolve_messenger_ingress_route",
                return_value=route,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.EnvironmentCredentialProvider.resolve",
                return_value="route-webhook-secret",
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress._vk_raw_message",
                return_value=("778899", "/start", "Анна"),
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.claim_inbound_event",
                return_value=True,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.ensure_channel_customer",
                return_value=identity,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.record_customer_contact",
                return_value=True,
            ) as record_contact,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.process_native_customer_interaction",
                return_value=True,
            ) as product_ui,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.record_customer_channel_message",
            ) as record_sales,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.complete_inbound_event",
            ),
        ):
            response = await canonical_vk_webhook(request)  # type: ignore[arg-type]

        self.assertEqual(200, response.status)
        record_contact.assert_called_once_with(
            business_id=route.business_id,
            platform="vk",
            external_subject="778899",
            display_name="Анна",
        )
        record_sales.assert_not_called()
        product_ui.assert_called_once()


    async def test_vk_product_callback_routes_to_customer_ui_not_sales(self) -> None:
        from types import SimpleNamespace
        from clientplatform.runtime.messenger_channel_ingress import canonical_vk_webhook

        route = MessengerIngressRoute(
            id=str(uuid4()), business_id=str(uuid4()), connection_id=str(uuid4()),
            platform=ConnectionPlatform.VK, external_route_id="424242",
            webhook_secret_reference="secret://env/CLIENTPLATFORM_SECRET_VK_WEBHOOK_TEST",
            confirmation_code_reference="secret://env/CLIENTPLATFORM_SECRET_VK_CONFIRMATION_TEST",
            status="active", created_by_member_id=str(uuid4()),
            created_at="2026-08-21T00:00:00+00:00",
            updated_at="2026-08-21T00:00:00+00:00",
        )
        request = _FakeRequest(
            {
                "type": "message_event", "group_id": "424242",
                "secret": "route-webhook-secret",
                "object": {
                    "event_id": "evt-ui-1", "user_id": 700001, "peer_id": 700001,
                    "payload": {"command": "cpi:programs:0"},
                },
            },
            route_id=route.id,
        )
        identity = SimpleNamespace(
            id=str(uuid4()), customer_id=str(uuid4()),
            external_subject="700001",
        )
        with (
            patch(
                "clientplatform.runtime.messenger_channel_ingress.resolve_messenger_ingress_route",
                return_value=route,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.EnvironmentCredentialProvider.resolve",
                return_value="route-webhook-secret",
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress._ack_vk_message_event",
                new=AsyncMock(),
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.claim_inbound_event",
                return_value=True,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.ensure_channel_customer",
                return_value=identity,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.record_customer_contact",
                return_value=True,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.process_native_customer_interaction",
                return_value=True,
            ) as product_ui,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.record_customer_channel_message"
            ) as sales,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.complete_inbound_event"
            ) as complete,
        ):
            response = await canonical_vk_webhook(request)  # type: ignore[arg-type]

        self.assertEqual(200, response.status)
        sales.assert_not_called()
        product_ui.assert_called_once()
        self.assertEqual(
            product_ui.call_args.kwargs["raw_text"], "cpi:programs:0"
        )
        complete.assert_called_once()

    async def test_max_free_text_remains_sales_signal_without_product_reply(self) -> None:
        from types import SimpleNamespace
        from clientplatform.runtime.messenger_channel_ingress import canonical_max_webhook

        route = MessengerIngressRoute(
            id=str(uuid4()), business_id=str(uuid4()), connection_id=str(uuid4()),
            platform=ConnectionPlatform.MAX, external_route_id="551001",
            webhook_secret_reference="secret://env/CLIENTPLATFORM_SECRET_MAX_WEBHOOK_TEST",
            confirmation_code_reference=None, status="active",
            created_by_member_id=str(uuid4()),
            created_at="2026-08-21T00:00:00+00:00",
            updated_at="2026-08-21T00:00:00+00:00",
        )
        request = _FakeRequest(
            {
                "update_type": "message_created", "update_id": 81001,
                "timestamp": 1787265000000,
                "message": {
                    "body": {"mid": "m-1", "text": "Хочу узнать стоимость"},
                    "sender": {"user_id": 700001, "first_name": "Анна"},
                },
            },
            route_id=route.id, headers={"X-Max-Bot-Api-Secret": "route-webhook-secret"},
        )
        identity = SimpleNamespace(
            id=str(uuid4()), customer_id=str(uuid4()), external_subject="700001"
        )
        with (
            patch(
                "clientplatform.runtime.messenger_channel_ingress.resolve_messenger_ingress_route",
                return_value=route,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.EnvironmentCredentialProvider.resolve",
                return_value="route-webhook-secret",
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.claim_inbound_event",
                return_value=True,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.ensure_channel_customer",
                return_value=identity,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.record_customer_contact",
                return_value=True,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.record_customer_channel_message",
                return_value="lead-1",
            ) as sales,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.process_native_customer_interaction"
            ) as product_ui,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.complete_inbound_event"
            ),
        ):
            response = await canonical_max_webhook(request)  # type: ignore[arg-type]

        self.assertEqual(200, response.status)
        sales.assert_called_once()
        self.assertEqual(
            sales.call_args.kwargs["message_text"], "Хочу узнать стоимость"
        )
        product_ui.assert_not_called()

    async def test_max_customer_link_success_queues_linked_menu_without_sales_capture(self) -> None:
        from types import SimpleNamespace
        from clientplatform.runtime.messenger_channel_ingress import canonical_max_webhook

        route = MessengerIngressRoute(
            id=str(uuid4()), business_id=str(uuid4()), connection_id=str(uuid4()),
            platform=ConnectionPlatform.MAX, external_route_id="551002",
            webhook_secret_reference="secret://env/CLIENTPLATFORM_SECRET_MAX_WEBHOOK_TEST",
            confirmation_code_reference=None, status="active",
            created_by_member_id=str(uuid4()),
            created_at="2026-08-21T00:00:00+00:00",
            updated_at="2026-08-21T00:00:00+00:00",
        )
        token = "A" * 32
        request = _FakeRequest(
            {
                "update_type": "message_created", "timestamp": 1787265001000,
                "message": {
                    "body": {"mid": "m-link", "text": f"cplink_{token}"},
                    "sender": {"user_id": 700002, "first_name": "Иван"},
                },
            },
            route_id=route.id, headers={"X-Max-Bot-Api-Secret": "route-webhook-secret"},
        )
        identity = SimpleNamespace(
            id=str(uuid4()), customer_id=str(uuid4()), external_subject="700002"
        )
        with (
            patch(
                "clientplatform.runtime.messenger_channel_ingress.resolve_messenger_ingress_route",
                return_value=route,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.EnvironmentCredentialProvider.resolve",
                return_value="route-webhook-secret",
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.claim_inbound_event",
                return_value=True,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.consume_customer_channel_link",
                return_value=identity,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.record_customer_contact",
                return_value=True,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.process_native_customer_interaction",
                return_value=True,
            ) as product_ui,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.record_customer_channel_message"
            ) as sales,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.complete_inbound_event"
            ),
        ):
            response = await canonical_max_webhook(request)  # type: ignore[arg-type]

        self.assertEqual(200, response.status)
        sales.assert_not_called()
        product_ui.assert_called_once()
        self.assertTrue(product_ui.call_args.kwargs["linked"])


class _FakeMaxSender:
    def __init__(self) -> None:
        self.legacy_ui: bool | None = None
        self.text: str | None = None

    async def send_text(self, external_subject: str, text: str, **kwargs):
        del external_subject
        self.text = text
        self.legacy_ui = kwargs.get("legacy_ui")
        return {"message_id": "max-message-1"}


class OmnichannelRuntimeTransportTests(unittest.IsolatedAsyncioTestCase):
    def test_vk_message_parser_is_dependency_light_and_fail_closed(self) -> None:
        self.assertEqual(
            ("778899", "book_now", None),
            vk_raw_message(
                {
                    "object": {
                        "message": {
                            "from_id": 778899,
                            "payload": {"command": "book_now"},
                        }
                    }
                }
            ),
        )
        self.assertIsNone(vk_raw_message({"object": {"message": {}}}))

    def test_native_callback_event_keys_include_provider_callback_identity(self) -> None:
        vk_first = {
            "type": "message_event",
            "object": {"event_id": "vk-event-1", "user_id": 778899},
        }
        vk_second = {
            "type": "message_event",
            "object": {"event_id": "vk-event-2", "user_id": 778899},
        }
        max_first = {
            "update_type": "message_callback",
            "timestamp": 1787259600000,
            "user": {"user_id": 778899},
            "callback": {"callback_id": "max-callback-1", "payload": "one"},
        }
        max_second = {
            "update_type": "message_callback",
            "timestamp": 1787259600000,
            "user": {"user_id": 778899},
            "callback": {"callback_id": "max-callback-2", "payload": "two"},
        }

        self.assertNotEqual(vk_event_key(vk_first), vk_event_key(vk_second))
        self.assertNotEqual(max_event_key(max_first), max_event_key(max_second))

    def test_official_max_message_callback_top_level_user_is_extracted(self) -> None:
        extracted = max_raw_message(
            {
                "update_type": "message_callback",
                "timestamp": 1787259600000,
                "user": {
                    "user_id": 778899,
                    "first_name": "Анна",
                    "last_name": "Иванова",
                    "is_bot": False,
                },
                "callback": {
                    "callback_id": "callback-1",
                    "payload": "book_now",
                },
            }
        )

        self.assertEqual(("778899", "book_now", "Анна Иванова"), extracted)

    async def test_canonical_max_text_explicitly_disables_legacy_metrotherapy_ui(self) -> None:
        sender = _FakeMaxSender()
        with patch(
            "clientplatform.runtime.messenger_provider_clients._max_sender",
            return_value=sender,
        ):
            message_id = await MaxRuntimeClient().send_text(
                token="provider-token",
                external_subject="max-user-1",
                text="1",
                idempotency_key="dispatch:max:1",
            )

        self.assertEqual("max-message-1", message_id)
        self.assertEqual("1", sender.text)
        self.assertIs(sender.legacy_ui, False)

    async def test_canonical_max_video_uses_native_video_provider_method(self) -> None:
        class Sender:
            def __init__(self) -> None:
                self.calls: list[tuple[str, Path]] = []

            async def send_video_file(self, external_subject: str, path: Path):
                self.calls.append((external_subject, path))
                return {"message_id": "max-video-1"}

        sender = Sender()
        materialized = Path("/tmp/clientplatform-max-video-test.mp4")
        with (
            patch(
                "clientplatform.runtime.messenger_provider_clients._max_sender",
                return_value=sender,
            ),
            patch(
                "clientplatform.runtime.messenger_provider_clients._materialize_media",
                new=AsyncMock(return_value=(materialized, False)),
            ),
        ):
            message_id = await MaxRuntimeClient().send_media(
                token="provider-token",
                external_subject="max-user-video",
                kind=__import__("clientplatform.domain.programs", fromlist=["ContentKind"]).ContentKind.VIDEO,
                media="https://media.example/video.mp4",
                idempotency_key="dispatch:max:video:1",
            )

        self.assertEqual("max-video-1", message_id)
        self.assertEqual([("max-user-video", materialized)], sender.calls)

    async def test_raw_max_sender_preserves_plain_text_without_legacy_ui(self) -> None:
        sender = MaxBotSender(
            token="provider-token",
            api_base_url=MAX_API2_BASE_URL,
        )
        with patch(
            "runtime.messenger_max_sender.json_request",
            return_value={"message": {"message_id": "max-raw-1"}},
        ) as request:
            result = await sender.send_text(
                "max-user-1",
                "1",
                legacy_ui=False,
            )

        self.assertEqual({"message_id": "max-raw-1"}, result)
        call = request.call_args
        self.assertIsNotNone(call)
        self.assertEqual({"text": "1"}, call.kwargs["payload"])
        self.assertIn("user_id=max-user-1", call.args[0])

    async def test_raw_max_sender_passes_provider_options_without_legacy_ui(self) -> None:
        sender = MaxBotSender(
            token="provider-token",
            api_base_url=MAX_API2_BASE_URL,
        )
        attachments = [
            {
                "type": "inline_keyboard",
                "payload": {"buttons": []},
            }
        ]
        with patch(
            "runtime.messenger_max_sender.json_request",
            return_value={"message": {"message_id": "max-raw-2"}},
        ) as request:
            result = await sender.send_text(
                "max-user-2",
                "Текст без подмены",
                legacy_ui=False,
                attachments=attachments,
                disable_link_preview=True,
                format="markdown",
                notify=False,
            )

        self.assertEqual({"message_id": "max-raw-2"}, result)
        call = request.call_args
        self.assertIsNotNone(call)
        self.assertIn("disable_link_preview=true", call.args[0])
        self.assertEqual(
            {
                "text": "Текст без подмены",
                "attachments": attachments,
                "format": "markdown",
                "notify": False,
            },
            call.kwargs["payload"],
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from runtime.messenger_max_sender import (
    MAX_API2_BASE_URL,
    MaxBotSender,
    _max_retryable_http_error,
)
from runtime.messenger_transport_errors import MessengerTransportError
from runtime.messenger_vk_sender import VkBotSender
from services.messenger.provider_transport import ProviderPermanentHTTPError


class NativeMessengerProviderProvisioningTests(unittest.IsolatedAsyncioTestCase):
    async def test_max_get_me_uses_official_api_and_authorization_header(self) -> None:
        sender = MaxBotSender(token="provider-token", api_base_url=MAX_API2_BASE_URL)
        with patch(
            "runtime.messenger_max_sender.json_request",
            return_value={
                "user_id": 7711,
                "first_name": "Бот",
                "username": "business_bot",
                "is_bot": True,
            },
        ) as request:
            identity = await sender.get_me()

        self.assertEqual(identity["user_id"], 7711)
        call = request.call_args
        self.assertEqual(call.args[0], f"{MAX_API2_BASE_URL}/me")
        self.assertEqual(call.kwargs["method"], "GET")
        self.assertEqual(call.kwargs["headers"], {"Authorization": "provider-token"})

    async def test_max_subscription_uses_secret_and_customer_event_types(self) -> None:
        sender = MaxBotSender(token="provider-token", api_base_url=MAX_API2_BASE_URL)
        with patch(
            "runtime.messenger_max_sender.json_request",
            return_value={"success": True},
        ) as request:
            await sender.ensure_webhook_subscription(
                url="https://client.example.test/clientplatform/webhooks/max/route",
                secret="safe_secret-12345",
            )

        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["secret"], "safe_secret-12345")
        self.assertEqual(
            payload["update_types"],
            ["message_created", "message_callback", "bot_started"],
        )
        self.assertEqual(request.call_args.kwargs["headers"], {"Authorization": "provider-token"})

    async def test_max_subscription_rejects_nonstandard_or_explicit_port(self) -> None:
        sender = MaxBotSender(token="provider-token", api_base_url=MAX_API2_BASE_URL)

        for url in (
            "https://client.example.test:8443/clientplatform/webhooks/max/route",
            "https://client.example.test:443/clientplatform/webhooks/max/route",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(
                ValueError,
                "default HTTPS port",
            ):
                await sender.ensure_webhook_subscription(
                    url=url,
                    secret="safe_secret-12345",
                )

    async def test_max_callback_ack_uses_official_answer_boundary(self) -> None:
        sender = MaxBotSender(token="provider-token", api_base_url=MAX_API2_BASE_URL)
        with patch(
            "runtime.messenger_max_sender.json_request",
            return_value={"success": True},
        ) as request:
            result = await sender.answer_callback(callback_id="callback / 1")

        self.assertTrue(result["success"])
        call = request.call_args
        self.assertEqual(
            call.args[0],
            f"{MAX_API2_BASE_URL}/answers?callback_id=callback%20%2F%201",
        )
        self.assertEqual(call.kwargs["method"], "POST")
        self.assertEqual(call.kwargs["payload"], {})
        self.assertEqual(call.kwargs["headers"], {"Authorization": "provider-token"})

    async def test_max_callback_ack_rejects_invalid_ids_before_provider_io(
        self,
    ) -> None:
        sender = MaxBotSender(token="provider-token", api_base_url=MAX_API2_BASE_URL)
        with patch(
            "runtime.messenger_max_sender.json_request",
            side_effect=AssertionError("invalid callback must not reach provider"),
        ):
            for callback_id in ("", "x" * 513, "callback\n1"):
                with self.subTest(callback_id=repr(callback_id)), self.assertRaisesRegex(
                    ValueError,
                    "callback id format",
                ):
                    await sender.answer_callback(callback_id=callback_id)

    async def test_max_callback_ack_sanitizes_provider_failures(self) -> None:
        sender = MaxBotSender(token="provider-token", api_base_url=MAX_API2_BASE_URL)
        with patch(
            "runtime.messenger_max_sender.json_request",
            side_effect=ProviderPermanentHTTPError(401),
        ), self.assertRaisesRegex(MessengerTransportError, "HTTP 401"):
            await sender.answer_callback(callback_id="callback-401")

        with patch(
            "runtime.messenger_max_sender.json_request",
            return_value={"success": False, "error": {"code": "denied"}},
        ), self.assertRaisesRegex(MessengerTransportError, "answer_callback failed"):
            await sender.answer_callback(callback_id="callback-denied")

    def test_max_rate_limit_classifier_rejects_malformed_or_other_statuses(
        self,
    ) -> None:
        malformed = type("MalformedProviderError", (OSError,), {"code": object()})()
        other = type("OtherProviderError", (OSError,), {"code": 503})()

        self.assertIsNone(_max_retryable_http_error("send_text", malformed))
        self.assertIsNone(_max_retryable_http_error("send_text", other))

    async def test_vk_verifies_expected_community(self) -> None:
        sender = VkBotSender(token="vk-token")
        sender._vk_method = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "response": {
                    "groups": [{"id": 238191212, "name": "Практика"}]
                }
            }
        )
        group = await sender.verify_community("238191212")
        self.assertEqual(group["name"], "Практика")
        sender._vk_method.assert_awaited_once_with(
            "groups.getById", {"group_id": 238191212}
        )

    async def test_vk_callback_server_reuses_url_and_enables_messages(self) -> None:
        sender = VkBotSender(token="vk-token", api_version="5.199")
        sender._vk_method = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {
                    "response": {
                        "items": [
                            {
                                "id": 77,
                                "url": "https://client.example.test/clientplatform/webhooks/vk/route",
                            }
                        ]
                    }
                },
                {"response": 1},
                {"response": 1},
            ]
        )
        server_id = await sender.ensure_callback_server(
            group_id="238191212",
            url="https://client.example.test/clientplatform/webhooks/vk/route",
            secret="callback-secret",
        )
        self.assertEqual(server_id, 77)
        calls = sender._vk_method.await_args_list
        self.assertEqual(calls[0].args[0], "groups.getCallbackServers")
        self.assertEqual(calls[1].args[0], "groups.editCallbackServer")
        self.assertEqual(calls[2].args[0], "groups.setCallbackSettings")
        self.assertEqual(calls[2].args[1]["message_new"], 1)
        self.assertEqual(calls[2].args[1]["message_event"], 1)


if __name__ == "__main__":
    unittest.main()

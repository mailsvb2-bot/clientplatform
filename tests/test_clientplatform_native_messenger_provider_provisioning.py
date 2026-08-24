from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from runtime.messenger_max_sender import MAX_API2_BASE_URL, MaxBotSender
from runtime.messenger_transport_errors import MessengerTransportError
from runtime.messenger_vk_sender import VkBotSender


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
        target = "https://client.example.test/clientplatform/webhooks/max/route"
        with patch(
            "runtime.messenger_max_sender.json_request",
            side_effect=[
                {"subscriptions": []},
                {"success": True},
                {"subscriptions": [{"url": target}]},
            ],
        ) as request:
            await sender.ensure_webhook_subscription(
                url=target,
                secret="safe_secret-12345",
            )

        calls = request.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0].kwargs["method"], "GET")
        self.assertEqual(calls[2].kwargs["method"], "GET")
        payload = calls[1].kwargs["payload"]
        self.assertEqual(payload["secret"], "safe_secret-12345")
        self.assertEqual(
            payload["update_types"],
            ["message_created", "message_callback", "bot_started"],
        )
        self.assertEqual(calls[1].kwargs["headers"], {"Authorization": "provider-token"})

    async def test_max_subscription_reuses_existing_url_without_duplicate_post(self) -> None:
        sender = MaxBotSender(token="provider-token", api_base_url=MAX_API2_BASE_URL)
        target = "https://client.example.test/clientplatform/webhooks/max/route"
        with patch(
            "runtime.messenger_max_sender.json_request",
            return_value={"subscriptions": [{"url": target + "/"}]},
        ) as request:
            result = await sender.ensure_webhook_subscription(
                url=target,
                secret="safe_secret-12345",
            )

        self.assertTrue(result["already_present"])
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["method"], "GET")
        self.assertIsNone(request.call_args.kwargs["payload"])

    async def test_max_subscription_reconciliation_accepts_provider_list_shapes(self) -> None:
        sender = MaxBotSender(token="provider-token", api_base_url=MAX_API2_BASE_URL)
        target = "https://client.example.test/clientplatform/webhooks/max/route"
        payloads = (
            [{"url": target}],
            {"items": [{"url": target}]},
            {"data": [{"url": target}]},
        )
        for payload in payloads:
            with self.subTest(payload=payload), patch(
                "runtime.messenger_max_sender.json_request",
                return_value=payload,
            ) as request:
                result = await sender.ensure_webhook_subscription(
                    url=target,
                    secret="safe_secret-12345",
                )
            self.assertTrue(result["already_present"])
            request.assert_called_once()

    async def test_max_provisioning_rejects_invalid_local_and_provider_contracts(self) -> None:
        sender = MaxBotSender(token="provider-token", api_base_url=MAX_API2_BASE_URL)

        with self.assertRaisesRegex(ValueError, "HTTPS"):
            await sender.ensure_webhook_subscription(
                url="http://client.example.test/hook",
                secret="safe_secret-12345",
            )
        with self.assertRaisesRegex(ValueError, "secret"):
            await sender.ensure_webhook_subscription(
                url="https://client.example.test/hook",
                secret="bad secret",
            )
        with self.assertRaisesRegex(ValueError, "update types"):
            await sender.ensure_webhook_subscription(
                url="https://client.example.test/hook",
                secret="safe_secret-12345",
                update_types=("",),
            )

        with patch(
            "runtime.messenger_max_sender.json_request",
            return_value={"error": {"code": "access_denied"}},
        ):
            with self.assertRaisesRegex(MessengerTransportError, "get_me"):
                await sender.get_me()
        with patch(
            "runtime.messenger_max_sender.json_request",
            return_value={"user_id": 0, "is_bot": False},
        ):
            with self.assertRaisesRegex(MessengerTransportError, "get_me"):
                await sender.get_me()
        with patch(
            "runtime.messenger_max_sender.json_request",
            side_effect=[
                {"subscriptions": []},
                {"success": False, "code": "rejected"},
            ],
        ):
            with self.assertRaisesRegex(MessengerTransportError, "subscription"):
                await sender.ensure_webhook_subscription(
                    url="https://client.example.test/hook",
                    secret="safe_secret-12345",
                )

        with patch(
            "runtime.messenger_max_sender.json_request",
            side_effect=[
                {"subscriptions": []},
                {"success": True},
                {"subscriptions": []},
            ],
        ):
            with self.assertRaisesRegex(MessengerTransportError, "subscription_verify"):
                await sender.ensure_webhook_subscription(
                    url="https://client.example.test/hook",
                    secret="safe_secret-12345",
                )

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

    async def test_vk_provisioning_validates_identity_and_provider_results(self) -> None:
        sender = VkBotSender(token="vk-token")

        with self.assertRaisesRegex(ValueError, "group id"):
            await sender.verify_community("not-a-group")
        with self.assertRaisesRegex(ValueError, "group id"):
            await sender.get_callback_confirmation_code("0")
        with self.assertRaisesRegex(ValueError, "group id"):
            await sender.ensure_callback_server(
                group_id="0",
                url="https://client.example.test/hook",
                secret="callback-secret",
            )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            await sender.ensure_callback_server(
                group_id="238191212",
                url="http://client.example.test/hook",
                secret="callback-secret",
            )
        with self.assertRaisesRegex(ValueError, "secret"):
            await sender.ensure_callback_server(
                group_id="238191212",
                url="https://client.example.test/hook",
                secret="",
            )

        sender._vk_method = AsyncMock(return_value={"response": []})  # type: ignore[method-assign]
        with self.assertRaisesRegex(MessengerTransportError, "groups_getbyid"):
            await sender.verify_community("238191212")

        sender._vk_method = AsyncMock(return_value={"response": {}})  # type: ignore[method-assign]
        with self.assertRaisesRegex(MessengerTransportError, "callback_confirmation"):
            await sender.get_callback_confirmation_code("238191212")

    async def test_vk_callback_server_creates_missing_provider_registration(self) -> None:
        sender = VkBotSender(token="vk-token", api_version="5.199")
        sender._vk_method = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"response": {"items": []}},
                {"response": {"server_id": 88}},
                {"response": 1},
            ]
        )

        server_id = await sender.ensure_callback_server(
            group_id="238191212",
            url="https://client.example.test/clientplatform/webhooks/vk/new-route",
            secret="callback-secret",
        )

        self.assertEqual(server_id, 88)
        calls = sender._vk_method.await_args_list
        self.assertEqual(calls[1].args[0], "groups.addCallbackServer")
        self.assertEqual(calls[2].args[0], "groups.setCallbackSettings")


if __name__ == "__main__":
    unittest.main()

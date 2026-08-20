from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from runtime.messenger_max_sender import MAX_API2_BASE_URL, MaxBotSender
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

from __future__ import annotations

import unittest
from unittest.mock import patch

from runtime.messenger_max_sender import MAX_API2_BASE_URL, MaxBotSender


class MaxWebhookReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_url_is_reconciled_with_current_secret_and_events(self) -> None:
        sender = MaxBotSender(token="provider-token", api_base_url=MAX_API2_BASE_URL)
        target = "https://client.example.test/clientplatform/webhooks/max/route"

        with patch(
            "runtime.messenger_max_sender.json_request",
            side_effect=[
                {"subscriptions": [{"url": target + "/"}]},
                {"success": True},
                {"subscriptions": [{"url": target}]},
            ],
        ) as request:
            result = await sender.ensure_webhook_subscription(
                url=target,
                secret="rotated_secret-67890",
            )

        self.assertTrue(result["success"])
        calls = request.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0].kwargs["method"], "GET")
        self.assertEqual(calls[1].kwargs["method"], "POST")
        self.assertEqual(calls[2].kwargs["method"], "GET")
        self.assertEqual(
            calls[1].kwargs["payload"],
            {
                "url": target,
                "update_types": [
                    "message_created",
                    "message_callback",
                    "bot_started",
                ],
                "secret": "rotated_secret-67890",
            },
        )
        self.assertEqual(
            calls[1].kwargs["headers"],
            {"Authorization": "provider-token"},
        )


if __name__ == "__main__":
    unittest.main()

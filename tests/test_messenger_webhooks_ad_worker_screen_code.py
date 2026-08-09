from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

import runtime.messenger_webhooks as messenger_webhooks


class AdvertisingWorkerScreenCodeTests(unittest.TestCase):
    def test_worker_remains_enabled_without_http_oauth_callback(self) -> None:
        with (
            patch.object(messenger_webhooks, "ad_connections_enabled", return_value=True),
            patch.object(
                messenger_webhooks,
                "yandex_direct_provider_configured",
                return_value=True,
            ),
            patch.object(messenger_webhooks, "ad_oauth_http_enabled", return_value=False),
        ):
            self.assertFalse(messenger_webhooks.ad_oauth_http_enabled())
            self.assertTrue(messenger_webhooks._ad_publication_worker_enabled())

    def test_worker_is_disabled_without_provider_configuration(self) -> None:
        with (
            patch.object(messenger_webhooks, "ad_connections_enabled", return_value=True),
            patch.object(
                messenger_webhooks,
                "yandex_direct_provider_configured",
                return_value=False,
            ),
        ):
            self.assertFalse(messenger_webhooks._ad_publication_worker_enabled())

    def test_runtime_uses_worker_flag_independently_from_oauth_http_flag(self) -> None:
        source = inspect.getsource(messenger_webhooks.start_messenger_webhook_runtime)
        self.assertIn(
            "ad_worker_enabled = _ad_publication_worker_enabled()",
            source,
        )
        self.assertIn("or ad_worker_enabled", source)
        self.assertIn("if ad_worker_enabled:", source)


if __name__ == "__main__":
    unittest.main()

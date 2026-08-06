from __future__ import annotations

import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from config.settings import settings
from scripts.clientplatform_messenger_channels_preflight import (
    inspect_messenger_channels,
)
from services.messenger.setup import build_setup_status


_FIELDS = {
    "TELEGRAM_BOT_USERNAME": "clientplatform_bot",
    "MESSENGER_WEBHOOK_ENABLED": False,
    "MESSENGER_PUBLIC_BASE_URL": "https://client.example.test",
    "TELEGRAM_WEBHOOK_PUBLIC_BASE_URL": "",
    "MAX_BOT_TOKEN": "",
    "MAX_WEBHOOK_SECRET": "",
    "MAX_BOT_NAME": "",
    "MAX_BOT_LINK_BASE": "",
    "VK_GROUP_ID": "",
    "VK_GROUP_TOKEN": "",
    "VK_CONFIRMATION_TOKEN": "",
    "VK_SECRET": "",
}


class MessengerChannelPreflightTests(unittest.TestCase):
    def _settings(self, **overrides: object) -> ExitStack:
        values = {**_FIELDS, **overrides}
        stack = ExitStack()
        for name, value in values.items():
            stack.enter_context(patch.object(settings, name, value))
        return stack

    def test_disabled_optional_channels_are_not_reported_missing(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "TELEGRAM_TRANSPORT": "polling",
                "MAX_WEBHOOK_ENABLED": "0",
                "VK_WEBHOOK_ENABLED": "0",
            },
            clear=False,
        ), self._settings():
            status = build_setup_status()
            inspected = inspect_messenger_channels()

        self.assertEqual(status.missing, ())
        self.assertEqual(status.warnings, ())
        self.assertTrue(status.max_ok)
        self.assertTrue(status.vk_ok)
        self.assertTrue(status.webhook_runtime_ok)
        self.assertTrue(inspected.ok)
        self.assertFalse(inspected.max_enabled)
        self.assertFalse(inspected.vk_enabled)

    def test_partial_disabled_channel_is_warning_not_startup_failure(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "TELEGRAM_TRANSPORT": "polling",
                "MAX_WEBHOOK_ENABLED": "0",
                "VK_WEBHOOK_ENABLED": "0",
            },
            clear=False,
        ), self._settings(MAX_BOT_TOKEN="partial-token"):
            status = build_setup_status()

        self.assertEqual(status.missing, ())
        self.assertEqual(len(status.warnings), 1)
        self.assertIn("MAX_WEBHOOK_ENABLED", status.warnings[0])

    def test_enabled_max_requires_full_production_contract(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "TELEGRAM_TRANSPORT": "polling",
                "MAX_WEBHOOK_ENABLED": "1",
                "VK_WEBHOOK_ENABLED": "0",
            },
            clear=False,
        ), self._settings(
            MAX_BOT_TOKEN="max-token",
            MAX_BOT_LINK_BASE="https://max.example.test/{payload}",
        ):
            status = build_setup_status()

        self.assertIn("MAX_WEBHOOK_SECRET", status.missing)
        self.assertFalse(status.max_ok)
        self.assertFalse(status.webhook_runtime_ok)

    def test_enabled_max_complete_contract_is_ready(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "TELEGRAM_TRANSPORT": "polling",
                "MAX_WEBHOOK_ENABLED": "1",
                "VK_WEBHOOK_ENABLED": "0",
            },
            clear=False,
        ), self._settings(
            MAX_BOT_TOKEN="max-token",
            MAX_WEBHOOK_SECRET="max-webhook-secret",
            MAX_BOT_LINK_BASE="https://max.example.test/{payload}",
        ):
            status = build_setup_status()

        self.assertEqual(status.missing, ())
        self.assertTrue(status.max_ok)
        self.assertTrue(status.webhook_runtime_ok)

    def test_enabled_vk_rejects_invalid_group_identity(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "TELEGRAM_TRANSPORT": "polling",
                "MAX_WEBHOOK_ENABLED": "0",
                "VK_WEBHOOK_ENABLED": "1",
            },
            clear=False,
        ), self._settings(
            VK_GROUP_ID="not-a-number",
            VK_GROUP_TOKEN="vk-token",
            VK_CONFIRMATION_TOKEN="vk-confirmation",
            VK_SECRET="vk-secret",
        ):
            status = build_setup_status()

        self.assertIn("VK_GROUP_ID must be a positive integer", status.missing)
        self.assertFalse(status.webhook_runtime_ok)


if __name__ == "__main__":
    unittest.main()

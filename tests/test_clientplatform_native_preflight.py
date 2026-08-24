from __future__ import annotations

import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from config.settings import settings
from scripts import clientplatform_messenger_channels_preflight as preflight
from services.messenger.setup import build_setup_status


_FIELDS = {
    "TELEGRAM_BOT_USERNAME": "",
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


class NativeOnlyMessengerStatusTests(unittest.TestCase):
    def _settings(self) -> ExitStack:
        stack = ExitStack()
        for name, value in _FIELDS.items():
            stack.enter_context(patch.object(settings, name, value))
        return stack

    def test_native_only_status_does_not_require_telegram_username(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "APP_ENV": "production",
                    "CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED": "0",
                    "CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED": "1",
                    "MAX_WEBHOOK_ENABLED": "0",
                    "VK_WEBHOOK_ENABLED": "0",
                },
                clear=False,
            ),
            self._settings(),
            patch.object(
                preflight,
                "_native_security_missing",
                return_value=(),
            ),
        ):
            status = build_setup_status()
            inspected = preflight.inspect_messenger_channels()

        self.assertTrue(status.telegram_ok)
        self.assertNotIn("TELEGRAM_BOT_USERNAME", status.missing)
        self.assertFalse(inspected.telegram_runtime_enabled)
        self.assertTrue(inspected.omnichannel_ready)
        self.assertTrue(inspected.ok)

    def test_enabled_telegram_runtime_keeps_username_requirement(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "APP_ENV": "production",
                    "CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED": "1",
                    "CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED": "1",
                    "MAX_WEBHOOK_ENABLED": "0",
                    "VK_WEBHOOK_ENABLED": "0",
                },
                clear=False,
            ),
            self._settings(),
        ):
            status = build_setup_status()

        self.assertFalse(status.telegram_ok)
        self.assertIn("TELEGRAM_BOT_USERNAME", status.missing)


if __name__ == "__main__":
    unittest.main()

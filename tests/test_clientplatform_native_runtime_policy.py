from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from clientplatform.runtime import dispatch_runtime
from runtime.telegram_transport import telegram_runtime_enabled


class TelegramRuntimePolicyTests(unittest.TestCase):
    def test_telegram_runtime_remains_enabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(telegram_runtime_enabled())

    def test_telegram_runtime_can_be_explicitly_disabled(self) -> None:
        with patch.dict(
            os.environ,
            {"CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED": "0"},
            clear=True,
        ):
            self.assertFalse(telegram_runtime_enabled())

    def test_invalid_telegram_runtime_flag_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {"CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED": "sometimes"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                telegram_runtime_enabled()


class NativeDispatchRuntimePolicyTests(unittest.TestCase):
    def test_canonical_omnichannel_enables_dispatch_without_control_bot(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED": "1",
                    "CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED": "",
                },
                clear=False,
            ),
            patch.object(dispatch_runtime, "control_bot_enabled", return_value=False),
        ):
            config = dispatch_runtime.dispatch_runtime_config()
        self.assertTrue(config.enabled)

    def test_explicit_dispatch_disable_overrides_omnichannel_default(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED": "1",
                    "CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED": "0",
                },
                clear=False,
            ),
            patch.object(dispatch_runtime, "control_bot_enabled", return_value=False),
        ):
            config = dispatch_runtime.dispatch_runtime_config()
        self.assertFalse(config.enabled)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import sqlite3
import unittest
from unittest.mock import patch

import config.settings as config_settings
from clientplatform.runtime import dispatch_runtime, native_runtime_policy
from core import startup_checks
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


class NativeOnlyAdmissionPolicyTests(unittest.TestCase):
    def _connection_db(self, *, active_telegram: int) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE connections(id TEXT PRIMARY KEY, platform TEXT NOT NULL, status TEXT NOT NULL)"
        )
        for index in range(active_telegram):
            conn.execute(
                "INSERT INTO connections(id,platform,status) VALUES(?, 'telegram', 'active')",
                (f"tg-{index}",),
            )
        conn.execute(
            "INSERT INTO connections(id,platform,status) VALUES('vk-1', 'vk', 'active')"
        )
        return conn

    def test_native_only_requires_canonical_omnichannel_ingress(self) -> None:
        conn = self._connection_db(active_telegram=0)
        try:
            with (
                patch.dict(
                    os.environ,
                    {"CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED": "0"},
                    clear=False,
                ),
                patch.object(native_runtime_policy, "get_db_ro", return_value=conn),
            ):
                with self.assertRaises(native_runtime_policy.NativeRuntimePolicyError):
                    native_runtime_policy.assert_native_only_runtime_policy()
        finally:
            conn.close()

    def test_native_only_rejects_active_telegram_connections(self) -> None:
        conn = self._connection_db(active_telegram=1)
        try:
            with (
                patch.dict(
                    os.environ,
                    {"CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED": "1"},
                    clear=False,
                ),
                patch.object(native_runtime_policy, "get_db_ro", return_value=conn),
            ):
                with self.assertRaises(native_runtime_policy.NativeRuntimePolicyError):
                    native_runtime_policy.assert_native_only_runtime_policy()
        finally:
            conn.close()

    def test_native_only_accepts_native_connections_without_active_telegram(self) -> None:
        conn = self._connection_db(active_telegram=0)
        try:
            with (
                patch.dict(
                    os.environ,
                    {"CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED": "1"},
                    clear=False,
                ),
                patch.object(native_runtime_policy, "get_db_ro", return_value=conn),
            ):
                native_runtime_policy.assert_native_only_runtime_policy()
        finally:
            conn.close()


class NativeOnlyProductionGuardTests(unittest.TestCase):
    def test_startup_checks_do_not_require_telegram_credentials_when_disabled(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED": "0",
                "HEALTHCHECK_ENABLED": "1",
                "METRO_DB_ENGINE": "postgres",
                "DATABASE_URL": "postgresql://runtime@example.invalid/clientplatform",
                "MESSENGER_WEBHOOK_ENABLED": "0",
            },
            clear=True,
        ):
            startup_checks._prod_ingress_checks()

    def test_startup_checks_keep_telegram_admin_guard_when_enabled(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED": "1",
                "HEALTHCHECK_ENABLED": "1",
                "METRO_DB_ENGINE": "postgres",
                "DATABASE_URL": "postgresql://runtime@example.invalid/clientplatform",
            },
            clear=True,
        ):
            with self.assertRaises(startup_checks.StartupCheckError):
                startup_checks._prod_ingress_checks()

    def test_settings_prod_guard_skips_bot_and_admin_only_when_telegram_disabled(self) -> None:
        with (
            patch.object(config_settings, "APP_ENV", "production"),
            patch.object(
                config_settings.settings,
                "CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED",
                False,
                create=True,
            ),
            patch.object(config_settings.settings, "MESSENGER_WEBHOOK_ENABLED", False),
            patch.object(config_settings.settings, "HEALTHCHECK_ENABLED", True),
            patch.dict(
                os.environ,
                {
                    "APP_ENV": "production",
                    "CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED": "0",
                    "PAYMENT_HTTP_ENABLED": "0",
                    "MAX_WEBHOOK_ENABLED": "0",
                    "VK_WEBHOOK_ENABLED": "0",
                    "HEALTHCHECK_ENABLED": "1",
                },
                clear=True,
            ),
        ):
            config_settings._fail_fast_prod_config()

    def test_settings_prod_guard_still_requires_bot_and_admin_when_enabled(self) -> None:
        with (
            patch.object(config_settings, "APP_ENV", "production"),
            patch.object(
                config_settings.settings,
                "CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED",
                True,
                create=True,
            ),
            patch.object(config_settings.settings, "BOT_TOKEN", ""),
            patch.object(config_settings.settings, "MESSENGER_WEBHOOK_ENABLED", False),
            patch.object(config_settings.settings, "HEALTHCHECK_ENABLED", True),
            patch.dict(
                os.environ,
                {
                    "APP_ENV": "production",
                    "CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED": "1",
                    "PAYMENT_HTTP_ENABLED": "0",
                    "MAX_WEBHOOK_ENABLED": "0",
                    "VK_WEBHOOK_ENABLED": "0",
                    "HEALTHCHECK_ENABLED": "1",
                },
                clear=True,
            ),
        ):
            with self.assertRaises(SystemExit) as raised:
                config_settings._fail_fast_prod_config()
        message = str(raised.exception)
        self.assertIn("BOT_TOKEN", message)
        self.assertIn("ADMIN_IDS", message)


if __name__ == "__main__":
    unittest.main()

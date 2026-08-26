from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

from config.settings import settings
from scripts import clientplatform_messenger_channels_preflight as preflight
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
            inspected = preflight.inspect_messenger_channels()

        self.assertEqual(status.missing, ())
        self.assertEqual(status.warnings, ())
        self.assertTrue(status.max_ok)
        self.assertTrue(status.vk_ok)
        self.assertTrue(status.webhook_runtime_ok)
        self.assertTrue(inspected.ok)
        self.assertFalse(inspected.max_enabled)
        self.assertFalse(inspected.vk_enabled)

    def test_canonical_omnichannel_ingress_needs_https_not_legacy_global_tokens(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "TELEGRAM_TRANSPORT": "polling",
                "CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED": "1",
                "MAX_WEBHOOK_ENABLED": "0",
                "VK_WEBHOOK_ENABLED": "0",
            },
            clear=False,
        ), self._settings(MESSENGER_PUBLIC_BASE_URL="https://client.example.test"), patch.object(
            preflight,
            "_native_security_missing",
            return_value=(),
        ):
            inspected = preflight.inspect_messenger_channels()

        self.assertTrue(inspected.omnichannel_enabled)
        self.assertTrue(inspected.omnichannel_ready)
        self.assertTrue(inspected.webhook_runtime_ready)
        self.assertEqual(inspected.missing, ())
        self.assertTrue(inspected.ok)

    def test_canonical_omnichannel_fails_closed_without_native_security(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED": "1",
                "MAX_WEBHOOK_ENABLED": "0",
                "VK_WEBHOOK_ENABLED": "0",
            },
            clear=False,
        ), self._settings(), patch.object(
            preflight,
            "_native_security_missing",
            return_value=("CLIENTPLATFORM managed credential age identity",),
        ):
            inspected = preflight.inspect_messenger_channels()

        self.assertFalse(inspected.omnichannel_ready)
        self.assertFalse(inspected.webhook_runtime_ready)
        self.assertFalse(inspected.ok)
        self.assertIn(
            "CLIENTPLATFORM managed credential age identity",
            inspected.missing,
        )

    def test_native_security_check_validates_signing_secret_identity_and_age_tools(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_IDENTITY_FILE": (
                    "/run/secrets/clientplatform-managed-bot/identity.txt"
                ),
                "CLIENTPLATFORM_MEDIA_SIGNING_SECRET_REFERENCE": (
                    "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"
                ),
            },
            clear=False,
        ), patch.object(
            preflight.EnvironmentCredentialProvider,
            "resolve",
            return_value="s" * 48,
        ), patch.object(
            preflight.AgeManagedBotCredentialVault,
            "validate_identity",
        ) as validate_identity, patch.object(
            preflight.shutil,
            "which",
            return_value="/usr/bin/age",
        ):
            missing = preflight._native_security_missing()

        self.assertEqual((), missing)
        validate_identity.assert_called_once_with()

    def test_canonical_omnichannel_ingress_rejects_insecure_production_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "TELEGRAM_TRANSPORT": "polling",
                "CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED": "1",
                "MAX_WEBHOOK_ENABLED": "0",
                "VK_WEBHOOK_ENABLED": "0",
            },
            clear=False,
        ), self._settings(MESSENGER_PUBLIC_BASE_URL="http://client.example.test"):
            inspected = preflight.inspect_messenger_channels()

        self.assertTrue(inspected.omnichannel_enabled)
        self.assertFalse(inspected.omnichannel_ready)
        self.assertFalse(inspected.webhook_runtime_ready)
        self.assertIn("MESSENGER_PUBLIC_BASE_URL must use https://", inspected.missing)
        self.assertFalse(inspected.ok)

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

    def test_enabled_max_rejects_insecure_production_base_url(self) -> None:
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
            MESSENGER_PUBLIC_BASE_URL="http://client.example.test",
            MAX_BOT_TOKEN="max-token",
            MAX_WEBHOOK_SECRET="max-webhook-secret",
            MAX_BOT_LINK_BASE="https://max.example.test/{payload}",
        ):
            status = build_setup_status()
            inspected = preflight.inspect_messenger_channels()

        self.assertIn(
            "MESSENGER_PUBLIC_BASE_URL must use https://",
            status.missing,
        )
        self.assertFalse(status.max_ok)
        self.assertFalse(status.webhook_runtime_ok)
        self.assertFalse(inspected.max_ready)
        self.assertFalse(inspected.webhook_runtime_ready)
        self.assertFalse(inspected.ok)

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
        self.assertFalse(status.vk_ok)
        self.assertFalse(status.webhook_runtime_ok)

    def test_enabled_vk_rejects_insecure_production_base_url(self) -> None:
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
            MESSENGER_PUBLIC_BASE_URL="http://client.example.test",
            VK_GROUP_ID="238191212",
            VK_GROUP_TOKEN="vk-token",
            VK_CONFIRMATION_TOKEN="vk-confirmation",
            VK_SECRET="vk-secret",
        ):
            status = build_setup_status()
            inspected = preflight.inspect_messenger_channels()

        self.assertIn(
            "MESSENGER_PUBLIC_BASE_URL must use https://",
            status.missing,
        )
        self.assertFalse(status.vk_ok)
        self.assertFalse(status.webhook_runtime_ok)
        self.assertFalse(inspected.vk_ready)
        self.assertFalse(inspected.webhook_runtime_ready)
        self.assertFalse(inspected.ok)

    def test_telegram_webhook_rejects_insecure_production_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "MAX_WEBHOOK_ENABLED": "0",
                "VK_WEBHOOK_ENABLED": "0",
            },
            clear=False,
        ), self._settings(
            TELEGRAM_WEBHOOK_PUBLIC_BASE_URL="http://client.example.test",
        ), patch(
            "services.messenger.setup.telegram_transport",
            return_value="webhook",
        ):
            status = build_setup_status()

        self.assertIn(
            "TELEGRAM_WEBHOOK_PUBLIC_BASE_URL must use https://",
            status.missing,
        )
        self.assertFalse(status.webhook_runtime_ok)

    def test_cli_success_emits_safe_json_and_success_marker(self) -> None:
        result = preflight.MessengerChannelPreflight(
            telegram_transport="polling",
            omnichannel_enabled=False,
            omnichannel_ready=True,
            max_enabled=False,
            max_ready=True,
            vk_enabled=False,
            vk_ready=True,
            webhook_runtime_ready=True,
            missing=(),
            warnings=("disabled optional channel",),
        )
        output = io.StringIO()
        with patch.object(
            preflight,
            "inspect_messenger_channels",
            return_value=result,
        ), redirect_stdout(output):
            exit_code = preflight.main()

        lines = output.getvalue().splitlines()
        payload = json.loads(lines[0])
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["telegram_transport"], "polling")
        self.assertEqual(lines[-1], "CLIENTPLATFORM_MESSENGER_CHANNELS_PREFLIGHT_OK")
        self.assertNotIn("secret", output.getvalue().lower())

    def test_cli_failure_emits_only_missing_names_and_nonzero_exit(self) -> None:
        result = preflight.MessengerChannelPreflight(
            telegram_transport="polling",
            omnichannel_enabled=False,
            omnichannel_ready=True,
            max_enabled=True,
            max_ready=False,
            vk_enabled=False,
            vk_ready=True,
            webhook_runtime_ready=False,
            missing=("MAX_WEBHOOK_SECRET",),
            warnings=(),
        )
        output = io.StringIO()
        with patch.object(
            preflight,
            "inspect_messenger_channels",
            return_value=result,
        ), redirect_stdout(output):
            exit_code = preflight.main()

        lines = output.getvalue().splitlines()
        payload = json.loads(lines[0])
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["missing"], ["MAX_WEBHOOK_SECRET"])
        self.assertEqual(
            lines[-1],
            "CLIENTPLATFORM_MESSENGER_CHANNELS_PREFLIGHT_FAILED:MAX_WEBHOOK_SECRET",
        )


if __name__ == "__main__":
    unittest.main()

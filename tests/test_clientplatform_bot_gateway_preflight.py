from __future__ import annotations

import unittest

from scripts.clientplatform_bot_gateway_preflight import validate_environment


class ClientPlatformBotGatewayPreflightTests(unittest.TestCase):
    @staticmethod
    def valid_env() -> dict[str, str]:
        return {
            "APP_ENV": "prod",
            "TELEGRAM_TRANSPORT": "polling",
            "TELEGRAM_WEBHOOK_ENABLED": "0",
            "TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED": "0",
            "CLIENTPLATFORM_BOT_GATEWAY_ENABLED": "1",
            "CLIENTPLATFORM_BOT_GATEWAY_BATCH_SIZE": "10",
            "CLIENTPLATFORM_BOT_GATEWAY_INTERVAL_SEC": "0.5",
            "CLIENTPLATFORM_BOT_GATEWAY_TICK_TIMEOUT_SEC": "30",
            "CLIENTPLATFORM_BOT_GATEWAY_LOCK_TTL_SEC": "300",
            "CLIENTPLATFORM_BOT_GATEWAY_MAX_ATTEMPTS": "5",
            "CLIENTPLATFORM_BOT_GATEWAY_PER_BOT_PER_MINUTE": "120",
            "CLIENTPLATFORM_BOT_GATEWAY_PER_BOT_QUEUE_LIMIT": "1000",
            "CLIENTPLATFORM_BOT_GATEWAY_MAX_PAYLOAD_BYTES": "262144",
            "CLIENTPLATFORM_BOT_GATEWAY_POLL_TIMEOUT_SEC": "20",
            "CLIENTPLATFORM_BOT_GATEWAY_RECONCILE_INTERVAL_SEC": "2",
        }

    def test_valid_production_gateway_environment_passes(self) -> None:
        self.assertEqual(validate_environment(self.valid_env()), [])

    def test_deployed_environment_fails_closed_when_gateway_is_disabled(self) -> None:
        env = self.valid_env()
        env["CLIENTPLATFORM_BOT_GATEWAY_ENABLED"] = "0"
        self.assertIn(
            "CLIENTPLATFORM_BOT_GATEWAY_ENABLED must be 1 in deployed environments",
            validate_environment(env),
        )

    def test_telegram_webhook_configuration_is_rejected(self) -> None:
        env = self.valid_env()
        env["TELEGRAM_TRANSPORT"] = "webhook"
        env["TELEGRAM_WEBHOOK_ENABLED"] = "1"
        errors = validate_environment(env)
        self.assertIn("TELEGRAM_TRANSPORT must be polling", errors)
        self.assertIn(
            "TELEGRAM_WEBHOOK_ENABLED must be 0 for polling-only Telegram",
            errors,
        )

    def test_managed_polling_does_not_require_messenger_webhook_server(self) -> None:
        env = self.valid_env()
        env["MESSENGER_WEBHOOK_ENABLED"] = "0"
        self.assertEqual(validate_environment(env), [])

    def test_limits_are_bounded(self) -> None:
        env = self.valid_env()
        env["CLIENTPLATFORM_BOT_GATEWAY_LOCK_TTL_SEC"] = "20"
        env["CLIENTPLATFORM_BOT_GATEWAY_POLL_TIMEOUT_SEC"] = "60"
        errors = validate_environment(env)
        self.assertIn(
            "CLIENTPLATFORM_BOT_GATEWAY_LOCK_TTL_SEC must be between 30 and 3600",
            errors,
        )
        self.assertIn(
            "CLIENTPLATFORM_BOT_GATEWAY_POLL_TIMEOUT_SEC must be between 1 and 50",
            errors,
        )


if __name__ == "__main__":
    unittest.main()

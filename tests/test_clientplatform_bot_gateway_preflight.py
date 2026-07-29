from __future__ import annotations

import unittest

from scripts.clientplatform_bot_gateway_preflight import validate_environment


class ClientPlatformBotGatewayPreflightTests(unittest.TestCase):
    @staticmethod
    def valid_env() -> dict[str, str]:
        return {
            "APP_ENV": "prod",
            "MESSENGER_WEBHOOK_ENABLED": "1",
            "TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED": "0",
            "HTTP_INGRESS_MAX_BODY_BYTES": "1048576",
            "CLIENTPLATFORM_BOT_GATEWAY_ENABLED": "1",
            "CLIENTPLATFORM_BOT_GATEWAY_PATH_PREFIX": "/clientplatform/managed-bots",
            "CLIENTPLATFORM_BOT_GATEWAY_BATCH_SIZE": "10",
            "CLIENTPLATFORM_BOT_GATEWAY_INTERVAL_SEC": "0.5",
            "CLIENTPLATFORM_BOT_GATEWAY_TICK_TIMEOUT_SEC": "30",
            "CLIENTPLATFORM_BOT_GATEWAY_LOCK_TTL_SEC": "300",
            "CLIENTPLATFORM_BOT_GATEWAY_MAX_ATTEMPTS": "5",
            "CLIENTPLATFORM_BOT_GATEWAY_PER_BOT_PER_MINUTE": "120",
            "CLIENTPLATFORM_BOT_GATEWAY_PER_BOT_QUEUE_LIMIT": "1000",
            "CLIENTPLATFORM_BOT_GATEWAY_MAX_PAYLOAD_BYTES": "262144",
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

    def test_path_cannot_expose_token_or_secret(self) -> None:
        for path in ("/bots/token/{id}", "/bots/secret", "relative"):
            with self.subTest(path=path):
                env = self.valid_env()
                env["CLIENTPLATFORM_BOT_GATEWAY_PATH_PREFIX"] = path
                self.assertTrue(validate_environment(env))

    def test_limits_are_bounded_and_ingress_must_cover_payload(self) -> None:
        env = self.valid_env()
        env["CLIENTPLATFORM_BOT_GATEWAY_LOCK_TTL_SEC"] = "20"
        env["CLIENTPLATFORM_BOT_GATEWAY_MAX_PAYLOAD_BYTES"] = "1048576"
        env["HTTP_INGRESS_MAX_BODY_BYTES"] = "262144"
        errors = validate_environment(env)
        self.assertIn(
            "CLIENTPLATFORM_BOT_GATEWAY_LOCK_TTL_SEC must be between 30 and 3600",
            errors,
        )
        self.assertIn(
            "HTTP ingress body limit must cover the Managed Bot Gateway payload limit",
            errors,
        )


if __name__ == "__main__":
    unittest.main()

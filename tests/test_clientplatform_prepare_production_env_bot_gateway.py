from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from scripts import clientplatform_prepare_production_env as prepare_env
from scripts.clientplatform_bot_gateway_preflight import validate_environment


_REQUIRED_ENV = """\
APP_ENV=prod
CLIENTPLATFORM_DOMAIN=clientplatform.example.test
CLIENTPLATFORM_STORAGE_BUCKET=clientplatform-production-test
CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT=https://s3.example.test
CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION=ru-1
CLIENTPLATFORM_SECRET_S3_ACCESS_KEY=access-secret
CLIENTPLATFORM_SECRET_S3_SECRET_KEY=secret-secret
"""


class ClientPlatformProductionBotGatewayPreparationTests(unittest.TestCase):
    def _prepare(self, extra: str = "") -> tuple[tuple[str, ...], str]:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "clientplatform.env"
            path.write_text(_REQUIRED_ENV + extra, encoding="utf-8")
            os.chmod(path, 0o600)
            added = prepare_env.prepare(path)
            payload = path.read_text(encoding="utf-8")
            return added, payload

    def test_prepare_adds_complete_bot_gateway_contract(self) -> None:
        added, payload = self._prepare()
        expected = {
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
        values: dict[str, str] = {}
        for line in payload.splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertIn(key, added)
                self.assertEqual(values.get(key), value)
        self.assertEqual(validate_environment(values), [])

    def test_prepare_rejects_invalid_existing_gateway_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "clientplatform.env"
            path.write_text(
                _REQUIRED_ENV + "CLIENTPLATFORM_BOT_GATEWAY_POLL_TIMEOUT_SEC=60\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(
                prepare_env.EnvironmentPreparationError,
                "invalid_bot_gateway_environment:.*POLL_TIMEOUT_SEC",
            ):
                prepare_env.prepare(path)

    def test_prepare_rejects_stale_telegram_webhook_mode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "clientplatform.env"
            path.write_text(
                _REQUIRED_ENV
                + "TELEGRAM_TRANSPORT=webhook\n"
                + "TELEGRAM_WEBHOOK_ENABLED=1\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(
                prepare_env.EnvironmentPreparationError,
                "invalid_bot_gateway_environment:.*TELEGRAM_TRANSPORT must be polling",
            ):
                prepare_env.prepare(path)


if __name__ == "__main__":
    unittest.main()

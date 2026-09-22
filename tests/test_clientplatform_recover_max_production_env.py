from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import clientplatform_recover_max_production_env as recovery


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _values(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if "=" not in raw or raw.lstrip().startswith("#"):
            continue
        key, value = raw.split("=", 1)
        result[key] = value
    return result


class MaxProductionEnvRecoveryTests(unittest.TestCase):
    def test_recover_uses_verified_historical_token_and_derives_missing_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_file = root / "clientplatform.env"
            _write_env(
                env_file,
                {
                    "APP_ENV": "prod",
                    "MESSENGER_PUBLIC_BASE_URL": "https://app.clientplatform.ru",
                    "MAX_API_BASE_URL": recovery.OFFICIAL_MAX_API_BASE,
                    "MAX_WEBHOOK_ENABLED": "0",
                },
            )
            _write_env(
                root / "clientplatform.env.before-previous",
                {
                    "MAX_BOT_TOKEN": "historical-token",
                    "MAX_WEBHOOK_SECRET": "old-secret-not-required",
                },
            )

            def fake_me(token: str, api_base: str, *, timeout_sec: int):
                self.assertEqual(token, "historical-token")
                self.assertEqual(api_base, recovery.OFFICIAL_MAX_API_BASE)
                self.assertEqual(timeout_sec, 7)
                return {"user_id": 123, "username": "clientplatform_max_bot"}

            with patch.object(recovery, "_max_me", side_effect=fake_me):
                report = recovery.recover(env_file, timeout_sec=7)

            self.assertTrue(report.ok)
            self.assertEqual(report.token_source, "historical_backup")
            self.assertTrue(report.webhook_secret_generated)
            self.assertTrue(report.bot_link_derived)
            self.assertTrue(report.bot_name_derived)

            values = _values(env_file)
            self.assertEqual(values["MAX_BOT_TOKEN"], "historical-token")
            self.assertEqual(values["MAX_WEBHOOK_ENABLED"], "1")
            self.assertEqual(values["MAX_BOT_NAME"], "clientplatform_max_bot")
            self.assertEqual(values["MAX_BOT_LINK_BASE"], recovery.CANONICAL_MAX_LINK_BASE)
            self.assertTrue(values["MAX_WEBHOOK_SECRET"])
            self.assertNotEqual(values["MAX_WEBHOOK_SECRET"], "old-secret-not-required")

    def test_recover_preserves_current_verified_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / "clientplatform.env"
            _write_env(
                env_file,
                {
                    "MESSENGER_PUBLIC_BASE_URL": "https://app.clientplatform.ru",
                    "MAX_API_BASE_URL": recovery.OFFICIAL_MAX_API_BASE,
                    "MAX_BOT_TOKEN": "current-token",
                    "MAX_WEBHOOK_SECRET": "current_secret",
                    "MAX_BOT_LINK_BASE": "https://max.ru/current-bot",
                    "MAX_BOT_NAME": "current-bot",
                    "MAX_WEBHOOK_ENABLED": "0",
                },
            )
            with patch.object(
                recovery,
                "_max_me",
                return_value={"user_id": 5, "username": "current-bot"},
            ):
                report = recovery.recover(env_file)

            values = _values(env_file)
            self.assertEqual(report.token_source, "current")
            self.assertFalse(report.webhook_secret_generated)
            self.assertFalse(report.bot_link_derived)
            self.assertFalse(report.bot_name_derived)
            self.assertEqual(values["MAX_WEBHOOK_SECRET"], "current_secret")
            self.assertEqual(values["MAX_BOT_LINK_BASE"], "https://max.ru/current-bot")
            self.assertEqual(values["MAX_WEBHOOK_ENABLED"], "1")

    def test_recover_fails_without_current_or_historical_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / "clientplatform.env"
            _write_env(
                env_file,
                {"MESSENGER_PUBLIC_BASE_URL": "https://app.clientplatform.ru"},
            )
            with self.assertRaisesRegex(
                recovery.MaxProductionRecoveryError,
                "max_bot_token_not_recoverable",
            ):
                recovery.recover(env_file)

    def test_recover_rejects_multiple_valid_bot_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_file = root / "clientplatform.env"
            _write_env(
                env_file,
                {"MESSENGER_PUBLIC_BASE_URL": "https://app.clientplatform.ru"},
            )
            _write_env(root / "clientplatform.env.before-one", {"MAX_BOT_TOKEN": "token-one"})
            _write_env(root / "clientplatform.env.before-two", {"MAX_BOT_TOKEN": "token-two"})

            def fake_me(token: str, api_base: str, *, timeout_sec: int):
                return {
                    "user_id": 1 if token == "token-one" else 2,
                    "username": "one" if token == "token-one" else "two",
                }

            with patch.object(recovery, "_max_me", side_effect=fake_me):
                with self.assertRaisesRegex(
                    recovery.MaxProductionRecoveryError,
                    "multiple_valid_max_bot_identities",
                ):
                    recovery.recover(env_file)

    def test_recover_rejects_nonofficial_provider_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / "clientplatform.env"
            _write_env(
                env_file,
                {
                    "MESSENGER_PUBLIC_BASE_URL": "https://app.clientplatform.ru",
                    "MAX_API_BASE_URL": "https://example.test",
                    "MAX_BOT_TOKEN": "token",
                },
            )
            with patch.object(recovery, "_max_me") as max_me:
                with self.assertRaisesRegex(
                    recovery.MaxProductionRecoveryError,
                    "max_api_base_not_official",
                ):
                    recovery.recover(env_file)
                max_me.assert_not_called()


if __name__ == "__main__":
    unittest.main()

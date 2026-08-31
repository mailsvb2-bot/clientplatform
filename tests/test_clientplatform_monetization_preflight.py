from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.clientplatform_monetization_preflight import validate_environment

ROOT = Path(__file__).resolve().parents[1]


class ClientPlatformMonetizationPreflightTests(unittest.TestCase):
    def test_valid_hard_token_contract_passes(self) -> None:
        env = {
            "TOKEN_ECONOMY_ENABLED": "1",
            "TOKEN_ENFORCEMENT_MODE": "hard",
            "PAYMENT_RECEIPT_EMAIL": "owner@example.test",
        }
        self.assertEqual(validate_environment(env), [])

    def test_soft_token_mode_fails_closed(self) -> None:
        env = {
            "TOKEN_ECONOMY_ENABLED": "1",
            "TOKEN_ENFORCEMENT_MODE": "soft",
            "PAYMENT_RECEIPT_EMAIL": "owner@example.test",
        }
        self.assertIn(
            "TOKEN_ENFORCEMENT_MODE must be hard in prod",
            validate_environment(env),
        )

    def test_missing_receipt_email_fails_closed(self) -> None:
        env = {
            "TOKEN_ECONOMY_ENABLED": "1",
            "TOKEN_ENFORCEMENT_MODE": "hard",
        }
        self.assertIn(
            "YOOKASSA_RECEIPT_EMAIL or PAYMENT_RECEIPT_EMAIL or ADMIN_EMAIL "
            "is required in prod",
            validate_environment(env),
        )

    def test_missing_receipt_email_passes_when_payment_http_is_disabled(self) -> None:
        env = {
            "APP_ENV": "prod",
            "PAYMENT_HTTP_ENABLED": "0",
            "TOKEN_ECONOMY_ENABLED": "1",
            "TOKEN_ENFORCEMENT_MODE": "hard",
        }
        self.assertEqual(validate_environment(env), [])

    def test_systemd_file_mode_can_import_canonical_payment_resolver(self) -> None:
        env = {
            **os.environ,
            "APP_ENV": "prod",
            "PAYMENT_HTTP_ENABLED": "0",
            "TOKEN_ECONOMY_ENABLED": "1",
            "TOKEN_ENFORCEMENT_MODE": "hard",
        }
        proc = subprocess.run(
            [sys.executable, "scripts/clientplatform_monetization_preflight.py", "--json"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"ok": true', proc.stdout)

    def test_disabled_token_economy_fails_closed(self) -> None:
        env = {
            "TOKEN_ECONOMY_ENABLED": "0",
            "TOKEN_ENFORCEMENT_MODE": "hard",
            "ADMIN_EMAIL": "owner@example.test",
        }
        self.assertIn(
            "TOKEN_ECONOMY_ENABLED must not be disabled in prod",
            validate_environment(env),
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from pathlib import Path

from scripts.check_clientplatform_product_purity import _forbidden_tokens


ROOT = Path(__file__).resolve().parents[1]
_CLIENTPLATFORM_POSTGRES_WORKFLOWS = (
    ".github/workflows/clientplatform-ad-spend-concurrency.yml",
    ".github/workflows/clientplatform-booking-concurrency.yml",
    ".github/workflows/clientplatform-bot-gateway.yml",
    ".github/workflows/clientplatform-bot-provisioning.yml",
    ".github/workflows/clientplatform-encrypted-backup.yml",
    ".github/workflows/clientplatform-production-isolation.yml",
)


class ClientPlatformCiDatabaseNamespaceTests(unittest.TestCase):
    def test_product_workflows_use_clientplatform_engine_variable(self) -> None:
        for relative in _CLIENTPLATFORM_POSTGRES_WORKFLOWS:
            with self.subTest(path=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("CLIENTPLATFORM_DB_ENGINE: postgres", source)
                retired_prefix = _forbidden_tokens()[2]
                self.assertNotIn(f"{retired_prefix}DB_ENGINE", source)

    def test_product_workflows_never_create_clientplatform_database(self) -> None:
        for relative in _CLIENTPLATFORM_POSTGRES_WORKFLOWS:
            with self.subTest(path=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("POSTGRES_DB: clientplatform\n", source)
                self.assertNotIn("@127.0.0.1:5432/clientplatform\n", source)

    def test_concurrency_workflows_have_isolated_product_databases(self) -> None:
        expected = {
            ".github/workflows/clientplatform-ad-spend-concurrency.yml": (
                "clientplatform_ad_spend_ci"
            ),
            ".github/workflows/clientplatform-booking-concurrency.yml": (
                "clientplatform_booking_ci"
            ),
            ".github/workflows/clientplatform-bot-gateway.yml": (
                "clientplatform_gateway_ci"
            ),
        }
        observed: set[str] = set()
        for relative, database_name in expected.items():
            with self.subTest(path=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(f"POSTGRES_DB: {database_name}", source)
                self.assertIn(f"-d {database_name}", source)
                self.assertIn(f"/{database_name}", source)
                self.assertIn(
                    f"CLIENTPLATFORM_DATABASE_NAME: {database_name}",
                    source,
                )
                observed.add(database_name)
        self.assertEqual(len(observed), len(expected))


if __name__ == "__main__":
    unittest.main()

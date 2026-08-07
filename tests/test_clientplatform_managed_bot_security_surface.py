from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ClientPlatformManagedBotSecuritySurfaceTests(unittest.TestCase):
    def test_sensitive_managed_bot_files_are_scanned(self) -> None:
        source = (ROOT / "scripts/critical_static_gate.py").read_text(encoding="utf-8")
        required = (
            "clientplatform/application/managed_bot_onboarding.py",
            "clientplatform/application/managed_bot_owner.py",
            "clientplatform/infrastructure/managed_bot_credentials.py",
            "clientplatform/infrastructure/managed_bot_onboarding_repository.py",
            "clientplatform/runtime/bot_provisioning.py",
            "clientplatform/runtime/secrets.py",
            "handlers/clientplatform_managed_bot_onboarding.py",
            "scripts/clientplatform_bot_gateway_preflight.py",
            "services/migrations/clientplatform_managed_bot_provider_v1.py",
        )
        for relative in required:
            with self.subTest(path=relative):
                self.assertIn(f'"{relative}"', source)

    def test_production_contract_is_fail_closed_by_default(self) -> None:
        env_source = (
            ROOT / "deploy/clientplatform/clientplatform.production.env.example"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "CLIENTPLATFORM_MANAGED_BOT_AUTO_PROVISIONING_ENABLED=0",
            env_source,
        )
        self.assertIn(
            "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_ALLOW_GENERATE=0",
            env_source,
        )
        self.assertIn(
            "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_IDENTITY_FILE=/run/secrets/clientplatform-managed-bot/identity.txt",
            env_source,
        )

    def test_production_compose_mounts_managed_bot_identity_read_only(self) -> None:
        compose = (ROOT / "deploy/clientplatform/compose.production.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            ":/run/secrets/clientplatform-managed-bot:ro",
            compose,
        )

    def test_managed_bot_ui_never_contains_secret_store_in_primary_copy(self) -> None:
        source = (
            ROOT / "handlers/clientplatform_managed_bot_onboarding.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("CLIENTPLATFORM_SECRET_", source)
        self.assertNotIn("secret-store", source.lower())
        self.assertIn("Никаких токенов копировать не потребуется", source)


if __name__ == "__main__":
    unittest.main()

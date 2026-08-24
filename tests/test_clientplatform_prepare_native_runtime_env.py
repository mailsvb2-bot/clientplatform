from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from scripts.clientplatform_prepare_production_env import prepare


_BASE_ENV = """\
CLIENTPLATFORM_DOMAIN=client.example.test
CLIENTPLATFORM_STORAGE_BUCKET=clientplatform-production
CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT=https://s3.example.test
CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION=test-1
CLIENTPLATFORM_SECRET_S3_ACCESS_KEY=access-key
CLIENTPLATFORM_SECRET_S3_SECRET_KEY=secret-key
"""


class NativeRuntimeProductionEnvPreparationTests(unittest.TestCase):
    def _write_env(self, payload: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "clientplatform.env"
        path.write_text(payload, encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def test_missing_runtime_flag_is_added_as_backward_compatible_enabled(self) -> None:
        path = self._write_env(_BASE_ENV)

        added = prepare(path)
        prepared = path.read_text(encoding="utf-8")

        self.assertIn("CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED", added)
        self.assertIn("CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED=1\n", prepared)

    def test_explicit_native_only_runtime_is_preserved(self) -> None:
        path = self._write_env(
            _BASE_ENV + "CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED=0\n"
        )

        added = prepare(path)
        prepared = path.read_text(encoding="utf-8")

        self.assertNotIn("CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED", added)
        self.assertIn("CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED=0\n", prepared)
        self.assertNotIn("CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED=1\n", prepared)

    def test_enabled_omnichannel_receives_secure_vault_and_signing_defaults(self) -> None:
        path = self._write_env(
            _BASE_ENV + "CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED=1\n"
        )

        prepare(path)
        prepared = path.read_text(encoding="utf-8")

        self.assertIn(
            "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_IDENTITY_FILE="
            "/run/secrets/clientplatform-managed-bot/identity.txt\n",
            prepared,
        )
        self.assertIn(
            "CLIENTPLATFORM_MEDIA_SIGNING_SECRET_REFERENCE="
            "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY\n",
            prepared,
        )

    def test_enabled_omnichannel_rejects_unsafe_identity_override(self) -> None:
        path = self._write_env(
            _BASE_ENV
            + "CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED=1\n"
            + "CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_IDENTITY_FILE=/tmp/identity.txt\n"
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "mismatched_clientplatform_managed_bot_credential_identity_file",
        ):
            prepare(path)


if __name__ == "__main__":
    unittest.main()

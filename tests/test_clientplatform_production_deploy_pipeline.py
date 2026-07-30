from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path

from scripts import clientplatform_prepare_production_env as prepare_env
from scripts import clientplatform_production_deploy as production_deploy


_REQUIRED_ENV = """\
CLIENTPLATFORM_DOMAIN=clientplatform.example.test
CLIENTPLATFORM_STORAGE_BUCKET=clientplatform-production-8493913
CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT=https://s3.example.test
CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION=ru-1
CLIENTPLATFORM_SECRET_S3_ACCESS_KEY=access-secret
CLIENTPLATFORM_SECRET_S3_SECRET_KEY=secret-secret
"""


class ProductionEnvironmentPreparationTests(unittest.TestCase):
    def test_prepare_is_idempotent_and_never_replaces_existing_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "clientplatform.env"
            path.write_text(
                _REQUIRED_ENV + "CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY=existing-key\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)

            added = prepare_env.prepare(path)
            first = path.read_text(encoding="utf-8")
            second_added = prepare_env.prepare(path)
            second = path.read_text(encoding="utf-8")

            self.assertIn("CLIENTPLATFORM_PROGRAM_MEDIA_INGEST_ENABLED", added)
            self.assertNotIn("CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY", added)
            self.assertIn("CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY=existing-key", first)
            self.assertEqual(second_added, ())
            self.assertEqual(first, second)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                path.with_name(path.name + ".before-current-main").stat().st_mode & 0o777,
                0o600,
            )

    def test_prepare_generates_signing_key_without_using_business_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "clientplatform.env"
            path.write_text(_REQUIRED_ENV, encoding="utf-8")
            os.chmod(path, 0o600)

            added = prepare_env.prepare(path)
            payload = path.read_text(encoding="utf-8")

            self.assertIn("CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY", added)
            signing_lines = [
                line
                for line in payload.splitlines()
                if line.startswith("CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY=")
            ]
            self.assertEqual(len(signing_lines), 1)
            self.assertGreater(len(signing_lines[0].split("=", 1)[1]), 40)
            self.assertNotIn("8493913", signing_lines[0])

    def test_prepare_rejects_world_readable_symlinked_or_mismatched_env(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            path = directory / "clientplatform.env"
            path.write_text(_REQUIRED_ENV, encoding="utf-8")
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(
                prepare_env.EnvironmentPreparationError,
                "permissions_must_be_0600",
            ):
                prepare_env.prepare(path)

            os.chmod(path, 0o600)
            link = directory / "linked.env"
            link.symlink_to(path)
            with self.assertRaisesRegex(
                prepare_env.EnvironmentPreparationError,
                "regular_file",
            ):
                prepare_env.prepare(link)

            path.write_text(
                _REQUIRED_ENV + "CLIENTPLATFORM_PUBLIC_BASE_URL=https://wrong.test\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(
                prepare_env.EnvironmentPreparationError,
                "mismatched_clientplatform_public_base_url",
            ):
                prepare_env.prepare(path)


class ProductionDeploymentContractTests(unittest.TestCase):
    def test_backup_checksum_is_streamed(self) -> None:
        payload = b"clientplatform" * 1024
        self.assertEqual(
            production_deploy._sha256_stream(io.BytesIO(payload)),
            __import__("hashlib").sha256(payload).hexdigest(),
        )

    def test_deploy_contract_orders_backup_before_recreate_and_keeps_rollback(self) -> None:
        source = Path(production_deploy.__file__).read_text(encoding="utf-8")
        backup_index = source.index("backup_reference =")
        build_index = source.index('_run([*compose, "build", "app", "backup"])')
        recreate_index = source.index('"--force-recreate", "app", "caddy"')

        self.assertLess(backup_index, build_index)
        self.assertLess(build_index, recreate_index)
        self.assertIn("production_readiness_timeout", source)
        self.assertIn("external_https_proof_failed", source)
        self.assertIn("rollback_tag", source)
        self.assertIn('"--no-build", "--force-recreate"', source)
        self.assertIn("CLIENTPLATFORM_PRODUCTION_DEPLOY_OK", source)
        self.assertNotIn("shell=True", source)

    def test_source_updater_preserves_env_and_requires_expected_sha_when_set(self) -> None:
        updater = (
            Path(production_deploy.ROOT)
            / "deploy"
            / "clientplatform"
            / "update-production.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('git fetch --no-tags --prune --depth 1 origin "$TARGET_REF"', updater)
        self.assertIn('TARGET_SHA=$(git rev-parse FETCH_HEAD)', updater)
        self.assertIn('EXPECTED_SHA=${CLIENTPLATFORM_EXPECTED_SHA:-}', updater)
        self.assertIn('"$TARGET_SHA" != "$EXPECTED_SHA"', updater)
        self.assertIn('git reset --hard "$TARGET_SHA"', updater)
        self.assertNotIn("git clean", updater)
        self.assertIn("clientplatform.env", updater)
        self.assertIn("clientplatform_production_deploy.py", updater)


if __name__ == "__main__":
    unittest.main()

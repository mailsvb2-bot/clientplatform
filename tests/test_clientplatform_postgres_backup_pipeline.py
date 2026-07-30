from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.clientplatform_postgres_backup_crypto import EncryptedBackupBundle
from scripts.clientplatform_postgres_backup_pipeline import run_backup_pipeline


class ClientPlatformPostgresBackupPipelineTests(unittest.TestCase):
    def _env(self, directory: Path) -> dict[str, str]:
        return {
            "DATABASE_URL": "postgresql://clientplatform_app:secret@postgres:5432/clientplatform",
            "CLIENTPLATFORM_BACKUP_DIR": str(directory),
            "CLIENTPLATFORM_BACKUP_RETENTION_DAYS": "30",
            "CLIENTPLATFORM_BACKUP_ENCRYPTION_REQUIRED": "1",
            "CLIENTPLATFORM_BACKUP_AGE_RECIPIENT": "age1" + "q" * 58,
            "CLIENTPLATFORM_POSTGRES_BACKUP_S3_ENABLED": "0",
        }

    def test_pipeline_rejects_disabled_encryption_before_dump(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env = self._env(Path(temp))
            env["CLIENTPLATFORM_BACKUP_ENCRYPTION_REQUIRED"] = "0"
            with patch(
                "scripts.clientplatform_postgres_backup_pipeline.create_backup"
            ) as create:
                with self.assertRaisesRegex(ValueError, "must be enabled"):
                    run_backup_pipeline(env)
                create.assert_not_called()

    def test_encryption_failure_removes_plaintext_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dump = root / "clientplatform-20260730T090218Z.dump"
            checksum = dump.with_suffix(".dump.sha256")
            metadata = dump.with_suffix(".dump.json")
            for path in (dump, checksum, metadata):
                path.write_bytes(b"plaintext")
            with (
                patch(
                    "scripts.clientplatform_postgres_backup_pipeline.create_backup",
                    return_value=dump,
                ),
                patch(
                    "scripts.clientplatform_postgres_backup_pipeline.encrypt_backup_bundle",
                    side_effect=RuntimeError("age unavailable"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "age unavailable"):
                    run_backup_pipeline(self._env(root))
            self.assertFalse(dump.exists())
            self.assertFalse(checksum.exists())
            self.assertFalse(metadata.exists())

    def test_optional_s3_stage_runs_only_after_encryption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dump = root / "clientplatform-20260730T090218Z.dump"
            ciphertext = root / "clientplatform-20260730T090218Z.dump.age"
            ciphertext.write_bytes(b"encrypted")
            bundle = EncryptedBackupBundle(
                ciphertext=ciphertext,
                checksum=ciphertext.with_suffix(".age.sha256"),
                metadata=ciphertext.with_suffix(".age.json"),
                ciphertext_sha256="a" * 64,
                plaintext_sha256="b" * 64,
            )
            env = self._env(root)
            env["CLIENTPLATFORM_POSTGRES_BACKUP_S3_ENABLED"] = "1"
            config = Mock(backup_bucket="clientplatform-backup")
            evidence = root / "evidence" / "latest.json"
            with (
                patch(
                    "scripts.clientplatform_postgres_backup_pipeline.create_backup",
                    return_value=dump,
                ),
                patch(
                    "scripts.clientplatform_postgres_backup_pipeline.encrypt_backup_bundle",
                    return_value=bundle,
                ) as encrypt,
                patch(
                    "scripts.clientplatform_postgres_backup_pipeline.config_from_env",
                    return_value=config,
                ),
                patch(
                    "scripts.clientplatform_postgres_backup_pipeline.StreamingS3Uploader",
                    return_value=Mock(),
                ),
                patch(
                    "scripts.clientplatform_postgres_backup_pipeline.upload_backup_bundle",
                    return_value=evidence,
                ) as upload,
                patch(
                    "scripts.clientplatform_postgres_backup_pipeline._prefix",
                    return_value="postgres",
                ),
                patch(
                    "scripts.clientplatform_postgres_backup_pipeline._evidence_dir",
                    return_value=root / "evidence",
                ),
            ):
                selected_bundle, selected_evidence = run_backup_pipeline(env)

            encrypt.assert_called_once()
            upload.assert_called_once()
            self.assertIs(selected_bundle, bundle)
            self.assertEqual(selected_evidence, evidence)
            self.assertEqual(upload.call_args.kwargs["ciphertext_path"], ciphertext)

    def test_compose_runs_pipeline_as_importable_module(self) -> None:
        root = Path(__file__).resolve().parents[1]
        compose = (root / "deploy/clientplatform/compose.production.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'entrypoint: ["python", "-m", "scripts.clientplatform_postgres_backup_pipeline"]',
            compose,
        )
        self.assertNotIn(
            'entrypoint: ["python", "scripts/clientplatform_postgres_backup_pipeline.py"]',
            compose,
        )


if __name__ == "__main__":
    unittest.main()

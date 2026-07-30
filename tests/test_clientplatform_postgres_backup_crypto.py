from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from scripts.clientplatform_postgres_backup_crypto import (
    decrypt_backup_bundle,
    encrypt_backup_bundle,
    recipient_from_env,
)

_RECIPIENT = "age1" + "q" * 58


def _plaintext_bundle(directory: Path, payload: bytes = b"postgres-backup") -> Path:
    dump = directory / "clientplatform-20260730T090218Z.dump"
    dump.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    dump.with_suffix(".dump.sha256").write_text(
        f"{digest}  {dump.name}\n",
        encoding="utf-8",
    )
    dump.with_suffix(".dump.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "created_at": "20260730T090218Z",
                "source_database": "clientplatform",
                "dump_file": dump.name,
                "sha256": digest,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return dump


class ClientPlatformPostgresBackupCryptoTests(unittest.TestCase):
    def test_encrypt_creates_owner_only_age_bundle_and_removes_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = b"sensitive-postgres-content"
            dump = _plaintext_bundle(root, payload)

            def fake_age(command: Sequence[str]) -> None:
                output = Path(command[command.index("--output") + 1])
                source = Path(command[-1])
                output.write_bytes(b"age-encrypted:" + source.read_bytes())

            bundle = encrypt_backup_bundle(
                dump,
                recipient=_RECIPIENT,
                run_command=fake_age,
                now=1785402138.0,
            )

            self.assertFalse(dump.exists())
            self.assertFalse(dump.with_suffix(".dump.sha256").exists())
            self.assertFalse(dump.with_suffix(".dump.json").exists())
            self.assertTrue(bundle.ciphertext.name.endswith(".dump.age"))
            self.assertEqual(os.stat(bundle.ciphertext).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(bundle.checksum).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(bundle.metadata).st_mode & 0o777, 0o600)
            metadata = json.loads(bundle.metadata.read_text(encoding="utf-8"))
            self.assertEqual(metadata["encryption"]["format"], "age-x25519")
            self.assertEqual(metadata["plaintext_sha256"], hashlib.sha256(payload).hexdigest())
            self.assertNotIn(_RECIPIENT, bundle.metadata.read_text(encoding="utf-8"))

    def test_failed_encryption_preserves_plaintext_and_removes_partial_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dump = _plaintext_bundle(root)

            def failing_age(_command: Sequence[str]) -> None:
                raise RuntimeError("simulated age failure")

            with self.assertRaisesRegex(RuntimeError, "simulated age failure"):
                encrypt_backup_bundle(
                    dump,
                    recipient=_RECIPIENT,
                    run_command=failing_age,
                )

            self.assertTrue(dump.is_file())
            self.assertTrue(dump.with_suffix(".dump.sha256").is_file())
            self.assertTrue(dump.with_suffix(".dump.json").is_file())
            self.assertFalse(dump.with_suffix(".dump.age").exists())

    def test_decrypt_verifies_identity_permissions_and_plaintext_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = b"restore-me"
            dump = _plaintext_bundle(root, payload)

            def fake_encrypt(command: Sequence[str]) -> None:
                output = Path(command[command.index("--output") + 1])
                output.write_bytes(b"age-encrypted:" + Path(command[-1]).read_bytes())

            encrypted = encrypt_backup_bundle(
                dump,
                recipient=_RECIPIENT,
                run_command=fake_encrypt,
            )
            identity = root / "age-identity.txt"
            identity.write_text("AGE-SECRET-KEY-TEST\n", encoding="utf-8")
            identity.chmod(0o600)

            def fake_decrypt(command: Sequence[str]) -> None:
                output = Path(command[command.index("--output") + 1])
                ciphertext = Path(command[-1]).read_bytes()
                output.write_bytes(ciphertext.removeprefix(b"age-encrypted:"))

            restored = decrypt_backup_bundle(
                encrypted.ciphertext,
                identity_file=identity,
                output_path=root / "restore.dump",
                run_command=fake_decrypt,
            )
            self.assertEqual(restored.read_bytes(), payload)
            self.assertEqual(os.stat(restored).st_mode & 0o777, 0o600)

            identity.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "must not be accessible"):
                decrypt_backup_bundle(
                    encrypted.ciphertext,
                    identity_file=identity,
                    output_path=root / "second.dump",
                    run_command=fake_decrypt,
                )

    def test_recipient_is_required_and_restricted_to_x25519_age_format(self) -> None:
        self.assertEqual(
            recipient_from_env({"CLIENTPLATFORM_BACKUP_AGE_RECIPIENT": _RECIPIENT}),
            _RECIPIENT,
        )
        for value in ("", "ssh-ed25519 AAAA", "age1short", "$(command)"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    recipient_from_env({"CLIENTPLATFORM_BACKUP_AGE_RECIPIENT": value})


if __name__ == "__main__":
    unittest.main()

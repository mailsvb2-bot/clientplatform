from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from scripts.clientplatform_postgres_backup_s3 import (
    FileChunkBody,
    StreamingS3Uploader,
    UploadedFile,
    _prefix,
    upload_backup_bundle,
)
from scripts.clientplatform_s3_replication import ReplicationConfig, ReplicationError


def _config(evidence_dir: Path) -> ReplicationConfig:
    return ReplicationConfig(
        endpoint="https://s3.twcstorage.ru",
        endpoint_host="s3.twcstorage.ru",
        endpoint_path="",
        region="ru-1",
        access_key="access-material",
        secret_key="secret-material",
        session_token="",
        source_bucket="clientplatform-production-8493913",
        backup_bucket="clientplatform-backup-8493913",
        evidence_dir=evidence_dir,
        timeout_seconds=30.0,
        max_copy_bytes=5_000_000_000,
    )


def _encrypted_bundle(directory: Path, payload: bytes = b"age-encrypted-postgres-backup") -> Path:
    ciphertext = directory / "clientplatform-20260730T090218Z.dump.age"
    ciphertext.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    ciphertext.with_suffix(".age.sha256").write_text(
        f"{digest}  {ciphertext.name}\n",
        encoding="utf-8",
    )
    ciphertext.with_suffix(".age.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "created_at": "2026-07-30T09:02:18+00:00",
                "source_database": "clientplatform",
                "encrypted_file": ciphertext.name,
                "ciphertext_sha256": digest,
                "plaintext_dump_file": "clientplatform-20260730T090218Z.dump",
                "plaintext_sha256": "a" * 64,
                "encryption": {
                    "format": "age-x25519",
                    "recipient_fingerprint": "b" * 64,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return ciphertext


class FakeUploader:
    def __init__(self) -> None:
        self.versioning = "Enabled"
        self.calls: list[tuple[str, str, Path, str, dict[str, str]]] = []

    def bucket_versioning(self, _bucket: str) -> str:
        return self.versioning

    def put_file(
        self,
        bucket: str,
        key: str,
        path: Path,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> UploadedFile:
        payload = path.read_bytes()
        item = UploadedFile(
            key=key,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        self.calls.append((bucket, key, path, content_type, dict(metadata)))
        return item


class FakeResponse:
    status = 200

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        del exc_type, exc, traceback
        return False

    def read(self, _limit: int = -1) -> bytes:
        return b""


class FakeControlClient:
    def __init__(self) -> None:
        self.remote_headers: dict[str, str] = {}

    def bucket_versioning(self, _bucket: str) -> str:
        return "Enabled"

    def head_object(self, _bucket: str, _key: str) -> dict[str, str]:
        return dict(self.remote_headers)


class ClientPlatformPostgresBackupS3Tests(unittest.TestCase):
    def test_file_chunk_body_is_bounded_and_reopenable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "large.dump.age"
            payload = b"a" * 200_000
            path.write_bytes(payload)
            body = FileChunkBody(path, chunk_bytes=65_536)
            first = list(body)
            second = list(body)
            self.assertEqual(b"".join(first), payload)
            self.assertEqual(first, second)
            self.assertTrue(all(0 < len(chunk) <= 65_536 for chunk in first))

    def test_streaming_put_uses_iterable_body_and_verifies_remote_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "clientplatform.dump.age"
            payload = b"stream-me" * 20_000
            path.write_bytes(payload)
            control = FakeControlClient()
            captured: dict[str, Any] = {}

            def opener(request, *, timeout: float):
                captured["timeout"] = timeout
                captured["body_type"] = type(request.data)
                chunks = list(request.data)
                captured["chunks"] = chunks
                captured["headers"] = {
                    str(name).lower(): str(value)
                    for name, value in request.header_items()
                }
                control.remote_headers = {
                    "content-length": str(len(payload)),
                    "x-amz-meta-clientplatform-sha256": hashlib.sha256(payload).hexdigest(),
                    "x-amz-meta-clientplatform-size": str(len(payload)),
                }
                return FakeResponse()

            uploader = StreamingS3Uploader(
                _config(Path(temp)),
                opener=opener,
                clock=lambda: datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc),
                chunk_bytes=65_536,
                control_client=control,  # type: ignore[arg-type]
            )
            uploaded = uploader.put_file(
                "clientplatform-backup-8493913",
                "postgres/2026/07/30/clientplatform.dump.age",
                path,
                content_type="application/octet-stream",
                metadata={"clientplatform-bundle-role": "ciphertext"},
            )

            self.assertIs(captured["body_type"], FileChunkBody)
            self.assertEqual(b"".join(captured["chunks"]), payload)
            self.assertTrue(all(len(chunk) <= 65_536 for chunk in captured["chunks"]))
            self.assertEqual(captured["headers"]["content-length"], str(len(payload)))
            self.assertIn("AWS4-HMAC-SHA256", captured["headers"]["authorization"])
            self.assertNotIn("secret-material", json.dumps(captured))
            self.assertEqual(uploaded.size, len(payload))
            self.assertEqual(uploaded.sha256, hashlib.sha256(payload).hexdigest())

    def test_bundle_uploads_ciphertext_checksum_then_metadata_commit_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ciphertext = _encrypted_bundle(root)
            uploader = FakeUploader()
            evidence = upload_backup_bundle(
                uploader,
                _config(root),
                ciphertext_path=ciphertext,
                prefix="postgres",
                evidence_dir=root / "evidence",
                now=datetime(2026, 7, 30, 9, 5, tzinfo=timezone.utc),
            )

            keys = [call[1] for call in uploader.calls]
            self.assertEqual(
                keys,
                [
                    "postgres/2026/07/30/clientplatform-20260730T090218Z.dump.age",
                    "postgres/2026/07/30/clientplatform-20260730T090218Z.dump.age.sha256",
                    "postgres/2026/07/30/clientplatform-20260730T090218Z.dump.age.json",
                ],
            )
            self.assertEqual(uploader.calls[-1][4]["clientplatform-bundle-role"], "metadata")
            self.assertTrue(
                all(call[4]["clientplatform-encryption"] == "age-x25519" for call in uploader.calls)
            )
            self.assertEqual(os.stat(evidence).st_mode & 0o777, 0o600)
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["encryption"], "age-x25519")
            self.assertEqual(len(payload["objects"]), 3)

    def test_plaintext_dump_is_rejected_before_any_remote_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plaintext = root / "clientplatform-20260730T090218Z.dump"
            plaintext.write_bytes(b"plaintext-must-not-leave-server")
            uploader = FakeUploader()
            with self.assertRaisesRegex(ValueError, "encrypted"):
                upload_backup_bundle(
                    uploader,
                    _config(root),
                    ciphertext_path=plaintext,
                    prefix="postgres",
                    evidence_dir=root / "evidence",
                )
            self.assertEqual(uploader.calls, [])

    def test_ciphertext_mismatch_fails_before_any_remote_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ciphertext = _encrypted_bundle(root)
            ciphertext.write_bytes(b"tampered")
            uploader = FakeUploader()
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                upload_backup_bundle(
                    uploader,
                    _config(root),
                    ciphertext_path=ciphertext,
                    prefix="postgres",
                    evidence_dir=root / "evidence",
                )
            self.assertEqual(uploader.calls, [])

    def test_versioning_is_required_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            uploader = FakeUploader()
            uploader.versioning = "Suspended"
            with self.assertRaisesRegex(
                ReplicationError,
                "backup_bucket_versioning_not_enabled",
            ):
                upload_backup_bundle(
                    uploader,
                    _config(root),
                    ciphertext_path=_encrypted_bundle(root),
                    prefix="postgres",
                    evidence_dir=root / "evidence",
                )
            self.assertEqual(uploader.calls, [])

    def test_prefix_is_restricted_to_safe_relative_object_paths(self) -> None:
        self.assertEqual(_prefix({}), "postgres")
        self.assertEqual(
            _prefix({"CLIENTPLATFORM_POSTGRES_BACKUP_S3_PREFIX": "database/postgres"}),
            "database/postgres",
        )
        for value in ("../postgres", "/", "postgres?secret=1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _prefix({"CLIENTPLATFORM_POSTGRES_BACKUP_S3_PREFIX": value})


if __name__ == "__main__":
    unittest.main()

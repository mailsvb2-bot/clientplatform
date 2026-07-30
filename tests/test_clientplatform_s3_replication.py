from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.clientplatform_s3_replication import (
    ObjectEntry,
    ReplicationConfig,
    ReplicationError,
    _authorization_headers,
    _canonical_query,
    _destination_matches,
    _normalize_etag,
    _write_evidence,
    config_from_env,
    prove_replication,
    sync_objects,
)


class FakeS3:
    def __init__(self) -> None:
        self.versioning = {
            "clientplatform-production-8493913": "Enabled",
            "clientplatform-backup-8493913": "Enabled",
        }
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}
        self.copy_calls: list[str] = []

    def bucket_versioning(self, bucket: str) -> str:
        return self.versioning[bucket]

    def list_objects(self, bucket: str, *, prefix: str = "") -> list[ObjectEntry]:
        result = []
        for (selected_bucket, key), (payload, headers) in sorted(self.objects.items()):
            if selected_bucket != bucket or not key.startswith(prefix):
                continue
            result.append(
                ObjectEntry(
                    key=key,
                    etag=_normalize_etag(headers["etag"]),
                    size=len(payload),
                    last_modified="2026-07-30T06:00:00Z",
                )
            )
        return result

    def head_object(self, bucket: str, key: str):
        item = self.objects.get((bucket, key))
        if item is None:
            return None
        payload, headers = item
        return {"content-length": str(len(payload)), **headers}

    def copy_object(
        self,
        *,
        source_bucket: str,
        backup_bucket: str,
        entry: ObjectEntry,
        source_headers,
    ) -> None:
        payload, headers = self.objects[(source_bucket, entry.key)]
        copied_headers = dict(headers)
        copied_headers["x-amz-meta-clientplatform-source-etag"] = entry.etag
        copied_headers["x-amz-meta-clientplatform-source-size"] = str(entry.size)
        self.objects[(backup_bucket, entry.key)] = (payload, copied_headers)
        self.copy_calls.append(entry.key)

    def put_object(
        self,
        bucket: str,
        key: str,
        payload: bytes,
        *,
        content_type: str,
        metadata,
    ) -> None:
        headers = {
            "etag": hashlib.md5(payload, usedforsecurity=False).hexdigest(),
            "content-type": content_type,
        }
        headers.update({f"x-amz-meta-{name}": value for name, value in metadata.items()})
        self.objects[(bucket, key)] = (payload, headers)

    def get_object(self, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)][0]

    def delete_object(self, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)


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


class ClientPlatformS3ReplicationTests(unittest.TestCase):
    def test_configuration_is_timeweb_compatible_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env = {
                "CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT": "https://s3.twcstorage.ru",
                "CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION": "ru-1",
                "CLIENTPLATFORM_SECRET_S3_ACCESS_KEY": "access-material",
                "CLIENTPLATFORM_SECRET_S3_SECRET_KEY": "secret-material",
                "CLIENTPLATFORM_STORAGE_BUCKET": "clientplatform-production-8493913",
                "CLIENTPLATFORM_S3_BACKUP_BUCKET": "clientplatform-backup-8493913",
                "CLIENTPLATFORM_S3_REPLICATION_EVIDENCE_DIR": temp,
            }
            config = config_from_env(env)
            self.assertEqual(config.endpoint_host, "s3.twcstorage.ru")
            self.assertEqual(config.region, "ru-1")
            for changes in (
                {"CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT": "http://s3.twcstorage.ru"},
                {"CLIENTPLATFORM_S3_BACKUP_BUCKET": env["CLIENTPLATFORM_STORAGE_BUCKET"]},
                {"CLIENTPLATFORM_S3_BACKUP_BUCKET": "shared-media"},
                {"CLIENTPLATFORM_SECRET_S3_SECRET_KEY": ""},
            ):
                with self.subTest(changes=changes):
                    invalid = dict(env)
                    invalid.update(changes)
                    with self.assertRaises(ReplicationError):
                        config_from_env(invalid)

    def test_sigv4_is_deterministic_and_does_not_expose_secret(self) -> None:
        headers = _authorization_headers(
            method="GET",
            host="s3.twcstorage.ru",
            path="/clientplatform-production-8493913/folder/file.mp3",
            query={"list-type": "2", "prefix": "folder/"},
            region="ru-1",
            access_key="ACCESS",
            secret_key="TOP-SECRET",
            session_token="",
            payload=b"",
            extra_headers={},
            now=datetime(2026, 7, 30, 6, 0, tzinfo=timezone.utc),
        )
        self.assertIn("Credential=ACCESS/20260730/ru-1/s3/aws4_request", headers["Authorization"])
        self.assertNotIn("TOP-SECRET", json.dumps(headers))
        self.assertEqual(
            _canonical_query({"prefix": "a/b c", "list-type": "2"}),
            "list-type=2&prefix=a%2Fb%20c",
        )

    def test_sync_copies_only_missing_or_changed_objects_and_never_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = FakeS3()
            config = _config(Path(temp))
            client.put_object(
                config.source_bucket,
                "media/a.mp3",
                b"alpha",
                content_type="audio/mpeg",
                metadata={},
            )
            client.put_object(
                config.source_bucket,
                "media/b.mp3",
                b"beta",
                content_type="audio/mpeg",
                metadata={},
            )
            client.copy_object(
                source_bucket=config.source_bucket,
                backup_bucket=config.backup_bucket,
                entry=client.list_objects(config.source_bucket)[0],
                source_headers=client.head_object(config.source_bucket, "media/a.mp3"),
            )
            client.objects[(config.backup_bucket, "orphan.txt")] = (
                b"retained",
                {"etag": "retained"},
            )
            client.copy_calls.clear()
            evidence = sync_objects(
                client,
                config,
                prefix="media/",
                started=datetime(2026, 7, 30, 6, 0, tzinfo=timezone.utc),
            )
            self.assertTrue(evidence["ok"])
            self.assertEqual(evidence["scanned"], 2)
            self.assertEqual(evidence["copied"], 1)
            self.assertEqual(evidence["skipped"], 1)
            self.assertEqual(evidence["verified"], 2)
            self.assertEqual(client.copy_calls, ["media/b.mp3"])
            self.assertIn((config.backup_bucket, "orphan.txt"), client.objects)

    def test_sync_fails_when_bucket_versioning_is_not_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = FakeS3()
            config = _config(Path(temp))
            client.versioning[config.backup_bucket] = "Suspended"
            with self.assertRaisesRegex(
                ReplicationError, "backup_bucket_versioning_not_enabled"
            ):
                sync_objects(client, config)

    def test_prove_verifies_payload_and_cleans_current_probe_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = FakeS3()
            config = _config(Path(temp))
            evidence = prove_replication(
                client,
                config,
                started=datetime(2026, 7, 30, 6, 0, tzinfo=timezone.utc),
            )
            self.assertTrue(evidence["ok"])
            self.assertTrue(evidence["probe_verified"])
            self.assertEqual(evidence["verified"], 1)
            self.assertFalse(
                any(
                    key.startswith(".clientplatform-replication-probe/")
                    for _, key in client.objects
                )
            )

    def test_destination_contract_requires_source_etag_and_size_metadata(self) -> None:
        entry = ObjectEntry("file", "abc", 3, "")
        self.assertTrue(
            _destination_matches(
                {
                    "content-length": "3",
                    "x-amz-meta-clientplatform-source-etag": "abc",
                    "x-amz-meta-clientplatform-source-size": "3",
                },
                entry,
            )
        )
        self.assertFalse(_destination_matches({"content-length": "3"}, entry))

    def test_deployment_contract_defaults_to_unproven_and_installs_timer(self) -> None:
        root = Path(__file__).resolve().parents[1]
        env_example = (
            root / "deploy/clientplatform/clientplatform.production.env.example"
        ).read_text(encoding="utf-8")
        service = (
            root / "deploy/clientplatform/clientplatform-s3-replication.service"
        ).read_text(encoding="utf-8")
        timer = (
            root / "deploy/clientplatform/clientplatform-s3-replication.timer"
        ).read_text(encoding="utf-8")
        compose = (
            root / "deploy/clientplatform/compose.production.yml"
        ).read_text(encoding="utf-8")
        runbook = (
            root / "docs/runbooks/CLIENTPLATFORM_TIMEWEB_S3_REPLICATION.md"
        ).read_text(encoding="utf-8")
        workflow = (
            root / ".github/workflows/clientplatform-production-isolation.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("CLIENTPLATFORM_S3_BACKUP_BUCKET=clientplatform-backup", env_example)
        self.assertIn(
            "CLIENTPLATFORM_S3_BACKUP_REPLICATION_ENABLED=0", env_example
        )
        self.assertIn("scripts/clientplatform_s3_replication.py sync", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("OnCalendar=*:0/15", timer)
        self.assertIn("s3-replication:", compose)
        self.assertIn(
            "CLIENTPLATFORM_S3_REPLICATION_PROOF_OK", runbook
        )
        self.assertIn(
            '"CLIENTPLATFORM_S3_BACKUP_REPLICATION_ENABLED": "1"', workflow
        )
        self.assertIn("tests/test_clientplatform_s3_replication.py", workflow)

    def test_evidence_is_atomic_sanitized_and_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = _config(Path(temp))
            path = _write_evidence(
                config,
                {
                    "ok": True,
                    "source_bucket": config.source_bucket,
                    "backup_bucket": config.backup_bucket,
                },
            )
            self.assertEqual(path.name, "latest.json")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(config.access_key, text)
            self.assertNotIn(config.secret_key, text)


if __name__ == "__main__":
    unittest.main()

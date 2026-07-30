from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.clientplatform_postgres_backup_freshness import evaluate_freshness

_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc).timestamp()


def _env(directory: Path, **overrides: str) -> dict[str, str]:
    values = {
        "CLIENTPLATFORM_POSTGRES_BACKUP_FRESHNESS_REQUIRED": "1",
        "CLIENTPLATFORM_POSTGRES_BACKUP_S3_ENABLED": "1",
        "CLIENTPLATFORM_POSTGRES_BACKUP_MAX_AGE_SECONDS": "10800",
        "CLIENTPLATFORM_POSTGRES_BACKUP_S3_EVIDENCE_DIR": str(directory),
    }
    values.update(overrides)
    return values


def _write_evidence(
    directory: Path,
    *,
    completed_at: str = "2026-07-30T11:00:00Z",
    mode: int = 0o600,
    **overrides: object,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": 1,
        "ok": True,
        "operation": "postgres_backup_s3_upload",
        "backup_bucket": "clientplatform-backup-8493913",
        "bundle": "clientplatform-20260730T110000Z",
        "bundle_sha256": "a" * 64,
        "encryption": "age-x25519",
        "completed_at": completed_at,
        "objects": [
            {
                "key": "postgres/2026/07/30/clientplatform-20260730T110000Z.dump.age",
                "size": 100,
                "sha256": "b" * 64,
            },
            {
                "key": "postgres/2026/07/30/clientplatform-20260730T110000Z.dump.age.sha256",
                "size": 128,
                "sha256": "c" * 64,
            },
            {
                "key": "postgres/2026/07/30/clientplatform-20260730T110000Z.dump.age.json",
                "size": 512,
                "sha256": "d" * 64,
            },
        ],
    }
    payload.update(overrides)
    path = directory / "latest.json"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


class ClientPlatformPostgresBackupFreshnessTests(unittest.TestCase):
    def test_disabled_gate_is_neutral_without_evidence(self) -> None:
        payload = evaluate_freshness(
            {
                "CLIENTPLATFORM_POSTGRES_BACKUP_FRESHNESS_REQUIRED": "0",
                "CLIENTPLATFORM_POSTGRES_BACKUP_S3_ENABLED": "0",
            },
            now=_NOW,
        )
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["required"])
        self.assertEqual(payload["errors"], [])

    def test_required_gate_rejects_disabled_offsite_upload(self) -> None:
        payload = evaluate_freshness(
            {
                "CLIENTPLATFORM_POSTGRES_BACKUP_FRESHNESS_REQUIRED": "1",
                "CLIENTPLATFORM_POSTGRES_BACKUP_S3_ENABLED": "0",
            },
            now=_NOW,
        )
        self.assertFalse(payload["ok"])
        self.assertIn("must be enabled", " ".join(payload["errors"]))

    def test_recent_owner_only_encrypted_evidence_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_evidence(root)
            payload = evaluate_freshness(_env(root), now=_NOW)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["age_seconds"], 3600.0)
        self.assertEqual(payload["errors"], [])

    def test_stale_backup_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_evidence(root, completed_at="2026-07-30T07:00:00Z")
            payload = evaluate_freshness(_env(root), now=_NOW)
        self.assertFalse(payload["ok"])
        self.assertIn("stale", " ".join(payload["errors"]))

    def test_future_timestamp_beyond_clock_skew_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_evidence(root, completed_at="2026-07-30T12:06:00Z")
            payload = evaluate_freshness(_env(root), now=_NOW)
        self.assertFalse(payload["ok"])
        self.assertIn("future", " ".join(payload["errors"]))

    def test_group_readable_evidence_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_evidence(root, mode=0o640)
            payload = evaluate_freshness(_env(root), now=_NOW)
        self.assertFalse(payload["ok"])
        self.assertIn("group/world", " ".join(payload["errors"]))

    def test_missing_or_symlinked_evidence_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing = evaluate_freshness(_env(root), now=_NOW)
            target = root / "actual.json"
            target.write_text("{}\n", encoding="utf-8")
            target.chmod(0o600)
            (root / "latest.json").symlink_to(target)
            symlinked = evaluate_freshness(_env(root), now=_NOW)
        self.assertFalse(missing["ok"])
        self.assertFalse(symlinked["ok"])
        self.assertIn("regular file", " ".join(missing["errors"]))
        self.assertIn("regular file", " ".join(symlinked["errors"]))

    def test_invalid_evidence_contract_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_evidence(
                root,
                ok=False,
                operation="wrong",
                encryption="none",
                objects=[],
            )
            payload = evaluate_freshness(_env(root), now=_NOW)
        rendered = " ".join(payload["errors"])
        self.assertFalse(payload["ok"])
        self.assertIn("not successful", rendered)
        self.assertIn("operation", rendered)
        self.assertIn("not age encrypted", rendered)
        self.assertIn("three bundle objects", rendered)

    def test_invalid_json_is_reported_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            root.mkdir(parents=True, exist_ok=True)
            evidence = root / "latest.json"
            evidence.write_text("{not-json", encoding="utf-8")
            evidence.chmod(0o600)
            payload = evaluate_freshness(_env(root), now=_NOW)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["errors"])

    def test_max_age_and_evidence_path_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_evidence(root)
            for value in ("not-a-number", "3599", "604801"):
                with self.subTest(value=value):
                    payload = evaluate_freshness(
                        _env(
                            root,
                            CLIENTPLATFORM_POSTGRES_BACKUP_MAX_AGE_SECONDS=value,
                        ),
                        now=_NOW,
                    )
                    self.assertFalse(payload["ok"])
            relative = evaluate_freshness(
                _env(
                    root,
                    CLIENTPLATFORM_POSTGRES_BACKUP_S3_EVIDENCE_DIR="relative/path",
                ),
                now=_NOW,
            )
        self.assertFalse(relative["ok"])
        self.assertIn("must be absolute", " ".join(relative["errors"]))

    def test_evidence_permissions_are_exactly_operator_controlled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence = _write_evidence(root)
            self.assertEqual(os.stat(evidence).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

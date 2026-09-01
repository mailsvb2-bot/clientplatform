from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services import disaster_recovery_status as dr


class BrokenDirectory:
    def __init__(self, *, exists_error: bool = False, scan_error: bool = False) -> None:
        self.exists_error = exists_error
        self.scan_error = scan_error

    def __str__(self) -> str:
        return "/broken/backups"

    def exists(self) -> bool:
        if self.exists_error:
            raise OSError("exists failed")
        return True

    def iterdir(self):
        if self.scan_error:
            raise OSError("scan failed")
        return iter(())


class BrokenCandidate:
    name = "backup.dump"

    def is_file(self) -> bool:
        raise OSError("candidate unreadable")


class CandidateDirectory(BrokenDirectory):
    def iterdir(self):
        return iter((BrokenCandidate(),))


def test_float_env_and_max_age_coercion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHASE9_FLOAT", raising=False)
    assert dr._float_env("PHASE9_FLOAT", 7.5) == 7.5

    monkeypatch.setenv("PHASE9_FLOAT", "12.25")
    assert dr._float_env("PHASE9_FLOAT", 7.5) == 12.25

    for raw in ("bad", "nan", "inf", "-inf"):
        monkeypatch.setenv("PHASE9_FLOAT", raw)
        assert dr._float_env("PHASE9_FLOAT", 7.5) == 7.5

    assert dr._coerce_max_age("3.5") == 3.5
    assert dr._coerce_max_age(object()) == dr.DEFAULT_MAX_BACKUP_AGE_HOURS
    assert dr._coerce_max_age(float("nan")) == dr.DEFAULT_MAX_BACKUP_AGE_HOURS


def test_status_marker_ok_and_dict() -> None:
    green = dr.DisasterRecoveryStatus(
        "/backups",
        "/backups/latest.dump",
        10,
        "2026-07-21T00:00:00+00:00",
        "abc",
        2,
        True,
        "GREEN",
        "restore_target_configured",
        latest_backup_age_seconds=5,
        latest_backup_fresh=True,
    )
    assert green.marker == "✅"
    assert green.ok is True
    assert green.to_dict()["latest_backup_sha256"] == "abc"

    red = dr.DisasterRecoveryStatus("/backups", None, 0, None, None, 0, False, "RED", "missing")
    assert red.marker == "🛑"
    assert red.ok is False

    unknown = dr.DisasterRecoveryStatus("/backups", "x", 1, None, None, 1, False, "BLUE", "unknown")
    assert unknown.marker == "⚠️"
    assert unknown.ok is False


def test_supported_hash_and_freshness(tmp_path: Path) -> None:
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"backup-data")
    sql_gz = tmp_path / "backup.sql.gz"
    sql_gz.write_bytes(b"compressed")
    other = tmp_path / "notes.txt"
    other.write_text("no", encoding="utf-8")

    assert dr._supported(dump) is True
    assert dr._supported(sql_gz) is True
    assert dr._supported(other) is False
    assert dr._supported(tmp_path) is False
    assert dr._hash(dump) == "185b0198441f4e8cdb882a49cb5a5d34108aada752d520d41b39bacdfa613e2c"

    future = datetime.now(tz=UTC) + timedelta(hours=1)
    age, fresh = dr._freshness(mtime_utc=future, max_age_hours=1)
    assert age == 0
    assert fresh is True

    old = datetime.now(tz=UTC) - timedelta(hours=2)
    age, fresh = dr._freshness(mtime_utc=old, max_age_hours=1)
    assert age >= 7190
    assert fresh is False
    assert dr._freshness(mtime_utc=old, max_age_hours=0)[1] is True


def test_missing_and_unreadable_backup_directories(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CLIENTPLATFORM_RESTORE_DRILL_DATABASE_URL", raising=False)
    monkeypatch.delenv("RESTORE_DATABASE_URL", raising=False)

    missing = dr.disaster_recovery_status(backup_dir=tmp_path / "missing")
    assert missing.status == "RED"
    assert missing.reason == "backup_dir_missing"

    unreadable_exists = dr.disaster_recovery_status(backup_dir=BrokenDirectory(exists_error=True))
    assert unreadable_exists.reason == "backup_dir_unreadable"

    unreadable_scan = dr.disaster_recovery_status(backup_dir=BrokenDirectory(scan_error=True))
    assert unreadable_scan.reason == "backup_dir_unreadable"

    empty = dr.disaster_recovery_status(backup_dir=tmp_path)
    assert empty.reason == "backup_missing"

    unreadable_candidate = dr.disaster_recovery_status(backup_dir=CandidateDirectory())
    assert unreadable_candidate.reason == "backup_unreadable"


def test_fresh_yellow_green_stale_and_hash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    old = tmp_path / "old.sql"
    old.write_bytes(b"old")
    latest = tmp_path / "latest.dump"
    latest.write_bytes(b"latest")
    ignored = tmp_path / "ignored.txt"
    ignored.write_text("ignore", encoding="utf-8")

    now = datetime.now(tz=UTC).timestamp()
    os.utime(old, (now - 7200, now - 7200))
    os.utime(latest, (now - 60, now - 60))

    monkeypatch.delenv("CLIENTPLATFORM_RESTORE_DRILL_DATABASE_URL", raising=False)
    monkeypatch.delenv("RESTORE_DATABASE_URL", raising=False)
    yellow = dr.disaster_recovery_status(backup_dir=tmp_path, include_hash=True, max_backup_age_hours=1)
    assert yellow.status == "YELLOW"
    assert yellow.reason == "backup_exists_restore_target_missing"
    assert yellow.latest_backup == str(latest)
    assert yellow.backup_count == 2
    assert yellow.latest_backup_sha256 == dr._hash(latest)
    assert yellow.latest_backup_fresh is True

    monkeypatch.setenv("RESTORE_DATABASE_URL", "postgresql://restore")
    green = dr.disaster_recovery_status(backup_dir=tmp_path, max_backup_age_hours=1)
    assert green.status == "GREEN"
    assert green.reason == "restore_target_configured"
    assert green.restore_target_configured is True

    stale = dr.disaster_recovery_status(backup_dir=tmp_path, max_backup_age_hours=0.001)
    assert stale.status == "RED"
    assert stale.reason.startswith("backup_stale_gt_")
    assert stale.latest_backup_fresh is False


def test_hash_failure_becomes_red(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backup = tmp_path / "latest.dump"
    backup.write_bytes(b"data")
    monkeypatch.setattr(dr, "_hash", lambda _path: (_ for _ in ()).throw(OSError("hash failed")))

    status = dr.disaster_recovery_status(backup_dir=tmp_path, include_hash=True)
    assert status.status == "RED"
    assert status.reason == "backup_hash_failed"
    assert status.latest_backup_sha256 is None


def test_format_admin_status_all_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    def status(level: str) -> dr.DisasterRecoveryStatus:
        return dr.DisasterRecoveryStatus(
            "/backups",
            "/backups/latest.dump" if level != "RED" else None,
            10,
            "2026-07-21T00:00:00+00:00" if level != "RED" else None,
            None,
            1 if level != "RED" else 0,
            level == "GREEN",
            level,
            {
                "GREEN": "restore_target_configured",
                "YELLOW": "backup_exists_restore_target_missing",
                "RED": "backup_missing",
            }[level],
            latest_backup_age_seconds=60 if level != "RED" else None,
            latest_backup_fresh=level != "RED",
        )

    for level, phrase in (
        ("GREEN", "можно запускать restore drill"),
        ("YELLOW", "drill target не настроен"),
        ("RED", "backup proof не закрыт"),
    ):
        monkeypatch.setattr(dr, "disaster_recovery_status", lambda **_kwargs: status(level))
        text = dr.format_disaster_recovery_status_for_admin()
        assert f"Статус: {status(level).marker} {level}" in text
        assert phrase in text

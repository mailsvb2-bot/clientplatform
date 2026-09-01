from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import backup_db, restore_db


def _write_value(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE demo(value TEXT)")
        conn.execute("INSERT INTO demo(value) VALUES (?)", (value,))
        conn.commit()
    finally:
        conn.close()


def _read_value(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM demo").fetchone()
        assert row is not None
        return str(row[0])
    finally:
        conn.close()


def _json_output(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def _apply_args(source: Path) -> list[str]:
    return [
        "--from-path",
        str(source),
        "--apply",
        "--confirm-service-stopped",
        restore_db.RESTORE_CONFIRMATION,
    ]


def test_backup_and_restore_roundtrip(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "data" / "data.db"
    _write_value(db_path, "before")

    monkeypatch.setattr(backup_db, "DB_PATH", db_path)
    monkeypatch.setattr(backup_db, "ROOT", tmp_path)
    monkeypatch.setattr(backup_db, "is_postgres_enabled", lambda: False)
    monkeypatch.setattr(restore_db, "DB_PATH", db_path)
    monkeypatch.setattr(restore_db, "ROOT", tmp_path)
    monkeypatch.setattr(restore_db, "is_postgres_enabled", lambda: False)

    assert backup_db.main() == 0
    capsys.readouterr()
    backup_file = restore_db._latest_backup()
    assert backup_file is not None and backup_file.exists()

    db_path.unlink()
    assert restore_db.main(["--from-path", str(backup_file)]) == 0
    dry_run = _json_output(capsys)
    assert dry_run["mode"] == "dry_run"
    assert dry_run["applied"] is False
    assert not db_path.exists()

    assert restore_db.main(_apply_args(backup_file)) == 0
    applied = _json_output(capsys)
    assert applied["mode"] == "apply"
    assert applied["applied"] is True
    assert applied["target_integrity_ok"] is True
    assert _read_value(db_path) == "before"


def test_backup_prunes_old_files(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for idx in range(5):
        (backup_dir / f"data_2026-01-01_00-00-0{idx}.db").write_text(
            str(idx),
            encoding="utf-8",
        )

    removed = backup_db._prune_old_backups(backup_dir, keep=2)
    assert removed == 3
    names = sorted(path.name for path in backup_dir.glob("data_*.db"))
    assert names == [
        "data_2026-01-01_00-00-03.db",
        "data_2026-01-01_00-00-04.db",
    ]


def test_restore_dry_run_never_changes_target_or_creates_safety_copy(
    tmp_path,
    monkeypatch,
    capsys,
):
    db_path = tmp_path / "data" / "data.db"
    backup_path = tmp_path / "backups" / "data_2026-01-01_00-00-00.db"
    _write_value(db_path, "current")
    _write_value(backup_path, "restored")

    monkeypatch.setattr(restore_db, "DB_PATH", db_path)
    monkeypatch.setattr(restore_db, "ROOT", tmp_path)
    monkeypatch.setattr(restore_db, "is_postgres_enabled", lambda: False)

    assert restore_db.main(["--from-path", str(backup_path)]) == 0
    payload = _json_output(capsys)

    assert payload["ok"] is True
    assert payload["mode"] == "dry_run"
    assert payload["applied"] is False
    assert payload["source_integrity_ok"] is True
    assert _read_value(db_path) == "current"
    assert list((tmp_path / "backups").glob("pre_restore_*.db")) == []


def test_restore_apply_requires_exact_service_stop_confirmation(
    tmp_path,
    monkeypatch,
    capsys,
):
    db_path = tmp_path / "data" / "data.db"
    backup_path = tmp_path / "backups" / "data_2026-01-01_00-00-00.db"
    _write_value(db_path, "current")
    _write_value(backup_path, "restored")

    monkeypatch.setattr(restore_db, "DB_PATH", db_path)
    monkeypatch.setattr(restore_db, "ROOT", tmp_path)
    monkeypatch.setattr(restore_db, "is_postgres_enabled", lambda: False)

    assert restore_db.main(["--from-path", str(backup_path), "--apply"]) == 2
    missing = _json_output(capsys)
    assert missing["error_code"] == "service_stop_confirmation_invalid"
    assert missing["applied"] is False

    assert (
        restore_db.main(
            [
                "--from-path",
                str(backup_path),
                "--apply",
                "--confirm-service-stopped",
                "wrong",
            ]
        )
        == 2
    )
    wrong = _json_output(capsys)
    assert wrong["error_code"] == "service_stop_confirmation_invalid"
    assert _read_value(db_path) == "current"
    assert list((tmp_path / "backups").glob("pre_restore_*.db")) == []


def test_restore_verify_safety_copy_and_atomic_target(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "data" / "data.db"
    backup_path = tmp_path / "backups" / "data_2026-01-01_00-00-00.db"
    _write_value(db_path, "current")
    _write_value(backup_path, "restored")

    monkeypatch.setattr(restore_db, "DB_PATH", db_path)
    monkeypatch.setattr(restore_db, "ROOT", tmp_path)
    monkeypatch.setattr(restore_db, "is_postgres_enabled", lambda: False)

    assert restore_db.main([*_apply_args(backup_path), "--verify"]) == 0
    payload = _json_output(capsys)

    assert payload["ok"] is True
    assert payload["source_integrity_ok"] is True
    assert payload["staged_integrity_ok"] is True
    assert payload["target_integrity_ok"] is True
    assert payload["rollback_performed"] is False
    assert _read_value(db_path) == "restored"
    safety = sorted((tmp_path / "backups").glob("pre_restore_*.db"))
    assert safety, "expected pre-restore safety backup to be created"
    assert _read_value(safety[-1]) == "current"
    assert list(db_path.parent.glob(f".{db_path.name}.restore-*.tmp")) == []


def test_restore_rolls_back_when_final_integrity_check_fails(
    tmp_path,
    monkeypatch,
    capsys,
):
    db_path = tmp_path / "data" / "data.db"
    backup_path = tmp_path / "backups" / "data_2026-01-01_00-00-00.db"
    _write_value(db_path, "current")
    _write_value(backup_path, "restored")

    monkeypatch.setattr(restore_db, "DB_PATH", db_path)
    monkeypatch.setattr(restore_db, "ROOT", tmp_path)
    monkeypatch.setattr(restore_db, "is_postgres_enabled", lambda: False)
    original_integrity_check = restore_db._integrity_check
    target_failure_injected = False

    def fail_once_for_installed_target(path: Path) -> None:
        nonlocal target_failure_injected
        if path.resolve() == db_path.resolve() and not target_failure_injected:
            target_failure_injected = True
            raise restore_db.RestoreDbError("forced_target_integrity_failure")
        original_integrity_check(path)

    monkeypatch.setattr(restore_db, "_integrity_check", fail_once_for_installed_target)

    assert restore_db.main(_apply_args(backup_path)) == 2
    payload = _json_output(capsys)

    assert payload["error_code"] == "restore_failed_rolled_back"
    assert payload["applied"] is True
    assert payload["rollback_performed"] is True
    assert _read_value(db_path) == "current"
    assert list(db_path.parent.glob(f".{db_path.name}.restore-*.tmp")) == []
    assert list(db_path.parent.glob(f".{db_path.name}.rollback-*.tmp")) == []


def test_restore_rejects_corrupt_source_before_target_mutation(
    tmp_path,
    monkeypatch,
    capsys,
):
    db_path = tmp_path / "data" / "data.db"
    corrupt = tmp_path / "backups" / "data_corrupt.db"
    _write_value(db_path, "current")
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"not-a-sqlite-database")

    monkeypatch.setattr(restore_db, "DB_PATH", db_path)
    monkeypatch.setattr(restore_db, "ROOT", tmp_path)
    monkeypatch.setattr(restore_db, "is_postgres_enabled", lambda: False)

    assert restore_db.main(_apply_args(corrupt)) == 2
    payload = _json_output(capsys)

    assert payload["error_code"] == "integrity_check_unavailable"
    assert payload["applied"] is False
    assert _read_value(db_path) == "current"
    assert list((tmp_path / "backups").glob("pre_restore_*.db")) == []


def test_restore_rejects_source_equal_to_target(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "data" / "data.db"
    _write_value(db_path, "current")

    monkeypatch.setattr(restore_db, "DB_PATH", db_path)
    monkeypatch.setattr(restore_db, "ROOT", tmp_path)
    monkeypatch.setattr(restore_db, "is_postgres_enabled", lambda: False)

    assert restore_db.main(_apply_args(db_path)) == 2
    payload = _json_output(capsys)

    assert payload["error_code"] == "source_equals_target"
    assert payload["applied"] is False
    assert _read_value(db_path) == "current"


def test_sqlite_backup_script_skips_in_postgres_mode(monkeypatch, capsys):
    monkeypatch.setattr(backup_db, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(
        backup_db,
        "redacted_db_target",
        lambda: "postgresql://clientplatform:***@127.0.0.1:5432/clientplatform",
    )

    assert backup_db.main() == 0

    out = capsys.readouterr().out
    assert "SKIP: CLIENTPLATFORM_DB_ENGINE=postgres uses pg_dump backups" in out
    assert "postgresql://clientplatform:***@127.0.0.1:5432/clientplatform" in out


def test_sqlite_restore_script_refuses_in_postgres_mode(monkeypatch):
    monkeypatch.setattr(restore_db, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(
        restore_db,
        "redacted_db_target",
        lambda: "postgresql://clientplatform:***@127.0.0.1:5432/clientplatform",
    )

    with pytest.raises(SystemExit) as exc:
        restore_db.main([])

    assert "REFUSE: CLIENTPLATFORM_DB_ENGINE=postgres uses pg_dump/psql restore" in str(exc.value)

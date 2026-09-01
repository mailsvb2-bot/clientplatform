from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import check_db


def _write_sqlite(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE alpha(value TEXT)")
        conn.execute("CREATE TABLE selected_plan(id INTEGER PRIMARY KEY)")
        conn.commit()


def test_explicit_sqlite_inspection_is_read_only_and_reports_tables(tmp_path: Path) -> None:
    path = tmp_path / "sample.db"
    _write_sqlite(path)

    payload = check_db.inspect_sqlite(path)

    assert payload["ok"] is True
    assert payload["mode"] == "sqlite_file"
    assert payload["integrity_ok"] is True
    assert payload["table_count"] == 2
    assert payload["tables"] == ["alpha", "selected_plan"]
    assert payload["selected_plan_present"] is True
    assert payload["error_code"] == ""
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()


def test_missing_sqlite_path_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"

    payload = check_db.inspect_sqlite(path)

    assert payload["ok"] is False
    assert payload["error_code"] == "sqlite_file_not_found"
    assert not path.exists()


def test_corrupt_sqlite_error_is_sanitized(tmp_path: Path) -> None:
    marker = "private-marker"
    path = tmp_path / f"{marker}.db"
    path.write_bytes(b"not-a-database")

    payload = check_db.inspect_sqlite(path)

    assert payload["ok"] is False
    assert payload["error_code"] == "sqlite_read_failed:SQLiteError"
    assert marker not in str(payload["error_code"])


def test_active_storage_report_uses_canonical_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Audit:
        def to_dict(self):
            return {
                "ok": True,
                "status": "GREEN",
                "active_engine": "postgres",
                "db_target": "postgresql://clientplatform:***@db/clientplatform",
                "legacy_sqlite_present": False,
                "repo_local_sqlite_present": False,
                "disallowed_direct_sqlite_connects": [],
            }

    import services.storage_legacy_audit as audit_module

    monkeypatch.setattr(audit_module, "storage_legacy_audit", lambda: _Audit())

    payload = check_db.active_storage_report()

    assert payload["mode"] == "active_storage"
    assert payload["status"] == "GREEN"
    assert payload["active_engine"] == "postgres"


def test_default_cli_does_not_probe_hardcoded_repo_data_db(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    hardcoded = tmp_path / "data.db"
    _write_sqlite(hardcoded)
    monkeypatch.setattr(check_db, "ROOT", tmp_path)
    monkeypatch.setattr(
        check_db,
        "active_storage_report",
        lambda: {
            "ok": True,
            "mode": "active_storage",
            "status": "GREEN",
            "active_engine": "postgres",
            "db_target": "postgresql://redacted",
            "legacy_sqlite_present": False,
            "repo_local_sqlite_present": False,
            "disallowed_direct_sqlite_connects": [],
        },
    )

    assert check_db.main(["--json", "--env-file", ""]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["mode"] == "active_storage"
    assert "tables" not in payload
    assert hardcoded.exists()


def test_explicit_sqlite_cli_strict_fails_on_missing_file(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing.db"

    assert (
        check_db.main(
            [
                "--sqlite-path",
                str(path),
                "--strict",
                "--json",
                "--env-file",
                "",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "sqlite_file_not_found"
    assert not path.exists()


def test_active_storage_cli_strict_uses_canonical_ok_flag(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        check_db,
        "active_storage_report",
        lambda: {
            "ok": False,
            "mode": "active_storage",
            "status": "RED",
            "active_engine": "sqlite",
            "db_target": "sqlite:///redacted",
            "legacy_sqlite_present": False,
            "repo_local_sqlite_present": False,
            "disallowed_direct_sqlite_connects": [],
        },
    )

    assert check_db.main(["--strict", "--json", "--env-file", ""]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "RED"


def test_env_loader_handles_export_quotes_and_comments(tmp_path: Path) -> None:
    env_file = tmp_path / "service.env"
    env_file.write_text(
        "# comment\nexport CLIENTPLATFORM_DB_ENGINE='postgres'\nDATABASE_URL=postgresql://db/example\n",
        encoding="utf-8",
    )

    loaded = check_db._load_env_file(env_file)

    assert loaded == {
        "CLIENTPLATFORM_DB_ENGINE": "postgres",
        "DATABASE_URL": "postgresql://db/example",
    }

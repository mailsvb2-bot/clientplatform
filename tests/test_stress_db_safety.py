from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from scripts import stress_db


def _payload(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def _table_names(path: Path) -> set[str]:
    with sqlite3.connect(str(path)) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def _journal_mode(path: Path) -> str:
    with sqlite3.connect(str(path)) as conn:
        row = conn.execute("PRAGMA journal_mode").fetchone()
    return str(row[0] if row else "").lower()


def test_default_cli_uses_unique_temporary_databases_and_removes_them(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths: list[Path] = []
    for _ in range(2):
        monkeypatch.setattr(
            sys,
            "argv",
            ["stress_db.py", "--workers", "1", "--iterations", "2"],
        )
        assert stress_db.main() == 0
        payload = _payload(capsys)
        path = Path(str(payload["db_path"]))
        paths.append(path)
        assert payload["ok"] is True
        assert payload["target_kind"] == "temporary"
        assert payload["actual_rows"] == 2
        assert payload["residual_rows"] == 0
        assert payload["table_removed"] is True
        assert payload["journal_mode_restored"] is True
        assert str(payload["cleanup_status"]).endswith(":temporary_db_removed")
        assert not path.exists()
        assert not Path(str(path) + "-wal").exists()
        assert not Path(str(path) + "-shm").exists()

    assert paths[0] != paths[1]


def test_custom_path_without_authorization_is_rejected_before_file_creation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    target = tmp_path / "custom.db"
    monkeypatch.setattr(sys, "argv", ["stress_db.py", "--db-path", str(target)])

    assert stress_db.main() == 2
    payload = _payload(capsys)

    assert payload == {
        "error_code": "custom_db_path_requires_allow_custom",
        "mutated": False,
        "ok": False,
        "target_kind": "rejected",
    }
    assert not target.exists()


def test_existing_path_requires_separate_authorization_and_remains_untouched(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    target = tmp_path / "existing.db"
    with sqlite3.connect(str(target)) as conn:
        conn.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        conn.execute("INSERT INTO sentinel(value) VALUES('preserve-me')")
        conn.commit()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stress_db.py",
            "--db-path",
            str(target),
            "--allow-custom-db-path",
        ],
    )

    assert stress_db.main() == 2
    payload = _payload(capsys)

    assert payload["error_code"] == "existing_db_path_requires_allow_existing"
    assert payload["mutated"] is False
    assert "stress_events" not in _table_names(target)
    with sqlite3.connect(str(target)) as conn:
        value = conn.execute("SELECT value FROM sentinel").fetchone()
    assert value == ("preserve-me",)


def test_configured_path_requires_dedicated_flag_and_exact_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    target = tmp_path / "configured.db"
    with sqlite3.connect(str(target)) as conn:
        conn.execute("CREATE TABLE sentinel(value INTEGER NOT NULL)")
        conn.execute("INSERT INTO sentinel(value) VALUES(7)")
        conn.commit()
    monkeypatch.setenv("CLIENTPLATFORM_DB_PATH", str(target))

    base_argv = [
        "stress_db.py",
        "--db-path",
        str(target),
        "--allow-custom-db-path",
        "--allow-existing-db-path",
    ]
    monkeypatch.setattr(sys, "argv", base_argv)
    assert stress_db.main() == 2
    assert _payload(capsys)["error_code"] == "configured_db_path_requires_allow_configured"

    monkeypatch.setattr(
        sys,
        "argv",
        [*base_argv, "--allow-configured-db-path", "--confirm-configured-db-path", "wrong"],
    )
    assert stress_db.main() == 2
    assert _payload(capsys)["error_code"] == "configured_db_path_confirmation_invalid"

    resolved = stress_db._authorize_custom_target(
        target,
        allow_custom=True,
        allow_existing=True,
        allow_configured=True,
        configured_confirmation=stress_db.CONFIGURED_DB_CONFIRMATION,
    )
    assert resolved == target.resolve()
    assert "stress_events" not in _table_names(target)


def test_authorized_existing_database_is_restored_without_residual_rows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    target = tmp_path / "diagnostic.db"
    with sqlite3.connect(str(target)) as conn:
        conn.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        conn.execute("INSERT INTO sentinel(value) VALUES('unchanged')")
        conn.commit()
    mode_before = _journal_mode(target)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stress_db.py",
            "--db-path",
            str(target),
            "--allow-custom-db-path",
            "--allow-existing-db-path",
            "--workers",
            "2",
            "--iterations",
            "5",
        ],
    )

    assert stress_db.main() == 0
    payload = _payload(capsys)

    assert payload["ok"] is True
    assert payload["target_kind"] == "custom"
    assert payload["db_existed_before"] is True
    assert payload["table_existed_before"] is False
    assert payload["actual_rows"] == 10
    assert payload["residual_rows"] == 0
    assert payload["table_removed"] is True
    assert payload["journal_mode_restored"] is True
    assert payload["cleanup_status"] == "run_rows_and_created_table_removed"
    assert _journal_mode(target) == mode_before
    assert "stress_events" not in _table_names(target)
    with sqlite3.connect(str(target)) as conn:
        value = conn.execute("SELECT value FROM sentinel").fetchone()
    assert value == ("unchanged",)


def test_incompatible_existing_stress_table_is_rejected_without_changes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    target = tmp_path / "incompatible.db"
    with sqlite3.connect(str(target)) as conn:
        conn.execute("CREATE TABLE stress_events(secret TEXT NOT NULL)")
        conn.execute("INSERT INTO stress_events(secret) VALUES('keep')")
        conn.commit()
    mode_before = _journal_mode(target)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stress_db.py",
            "--db-path",
            str(target),
            "--allow-custom-db-path",
            "--allow-existing-db-path",
        ],
    )

    assert stress_db.main() == 2
    payload = _payload(capsys)

    assert payload["error_code"] == "existing_stress_table_schema_incompatible"
    assert payload["mutated"] is False
    assert _journal_mode(target) == mode_before
    with sqlite3.connect(str(target)) as conn:
        rows = conn.execute("SELECT secret FROM stress_events").fetchall()
    assert rows == [("keep",)]


def test_cli_bounds_worker_and_iteration_counts() -> None:
    assert stress_db._bounded_positive(-5, maximum=64) == 1
    assert stress_db._bounded_positive(999, maximum=64) == 64
    assert stress_db._bounded_positive(0, maximum=100_000) == 1
    assert stress_db._bounded_positive(500_000, maximum=100_000) == 100_000

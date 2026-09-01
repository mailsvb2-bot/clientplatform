from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from services.validators import db as db_validator
from services.validators.base import ValidationError


class Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def fetchall(self) -> list[Any]:
        return self.rows


class FakeConnection:
    def __init__(self, tables: set[str], columns: set[str] | None = None) -> None:
        self.tables = tables
        self.columns = columns or set()
        self.calls: list[str] = []

    def execute(self, sql: str) -> Result:
        self.calls.append(sql)
        if "sqlite_master" in sql:
            return Result([(name,) for name in sorted(self.tables)])
        if sql.startswith("PRAGMA table_info(business_payments)"):
            return Result([(index, name) for index, name in enumerate(sorted(self.columns))])
        raise AssertionError(sql)


@contextmanager
def connection(conn: FakeConnection) -> Iterator[FakeConnection]:
    yield conn


def required_tables() -> set[str]:
    from services.db.schema.readiness import READY_TABLES

    return set(READY_TABLES)


def required_payment_columns() -> set[str]:
    return {"id", "business_id", "status", "amount_minor", "currency"}


def test_excluded_scan_path_inside_and_outside_project(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    assert db_validator._is_excluded_scan_path(root / ".venv" / "x.py", root) is True
    assert db_validator._is_excluded_scan_path(root / "services" / "x.py", root) is False
    outside = tmp_path / "node_modules" / "x.py"
    assert db_validator._is_excluded_scan_path(outside, root) is True


def test_validate_no_real_db_clean_strict_and_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(db_validator, "PROJECT_ROOT", tmp_path)
    db_validator.validate_no_real_db()

    live_db = tmp_path / "data" / "data.db"
    live_db.parent.mkdir(parents=True)
    live_db.write_bytes(b"state")
    with pytest.raises(ValidationError, match="stateful DB"):
        db_validator.validate_no_real_db(strict=True)
    db_validator.validate_no_real_db(strict=False)

    live_db.unlink()
    root_db = tmp_path / "data.db"
    root_db.write_bytes(b"root-state")
    with pytest.raises(ValidationError, match="size=10"):
        db_validator.validate_no_real_db(strict=True)


def test_validate_no_real_db_stat_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(db_validator, "PROJECT_ROOT", tmp_path)
    path = tmp_path / "data.db"
    path.write_bytes(b"state")
    original_stat = Path.stat

    def broken_stat(self: Path, *args: Any, **kwargs: Any):
        if self == path:
            raise OSError("stat failed")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", broken_stat)
    with pytest.raises(ValidationError, match="size=-1"):
        db_validator.validate_no_real_db(strict=True)


def install_schema_connection(
    monkeypatch: pytest.MonkeyPatch,
    conn: FakeConnection,
) -> None:
    monkeypatch.setattr(db_validator, "get_connection", lambda: connection(conn))
    monkeypatch.setattr(db_validator, "DB_PATH", Path("runtime.db"))


def test_table_columns_and_valid_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConnection(required_tables(), required_payment_columns())
    install_schema_connection(monkeypatch, conn)
    assert db_validator._table_columns(conn, "business_payments") == required_payment_columns()
    db_validator.validate_db_schema(strict=True)


def test_validate_db_schema_missing_tables_strict_and_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = FakeConnection({"users"})
    install_schema_connection(monkeypatch, conn)
    with pytest.raises(ValidationError, match="missing required tables"):
        db_validator.validate_db_schema(strict=True)

    conn = FakeConnection({"users"})
    install_schema_connection(monkeypatch, conn)
    db_validator.validate_db_schema(strict=False)


def test_validate_db_schema_missing_payment_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    columns = required_payment_columns() - {"amount_minor", "currency"}
    conn = FakeConnection(required_tables(), columns)
    install_schema_connection(monkeypatch, conn)
    with pytest.raises(ValidationError, match="business_payments missing columns"):
        db_validator.validate_db_schema(strict=True)

    conn = FakeConnection(required_tables(), columns)
    install_schema_connection(monkeypatch, conn)
    db_validator.validate_db_schema(strict=False)


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_schema_decomposition_allows_canonical_and_ignored_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(db_validator, "PROJECT_ROOT", tmp_path)
    write(tmp_path, "services/schema_core.py", "from services.schema_tables import build\n")
    write(tmp_path, "services/schema_tables.py", "CREATE TABLE allowed(id INTEGER)\n")
    write(tmp_path, "services/db/schema/users.py", "ALTER TABLE allowed ADD COLUMN x TEXT\n")
    write(tmp_path, "services/runtime_reader.py", "query = 'SELECT * FROM schema_migrations'\n")
    write(tmp_path, "tests/test_bad.py", "from services.schema_tables import build\nDROP TABLE x\n")
    write(tmp_path, ".venv/lib/bad.py", "from services.schema_tables import build\nDROP TABLE x\n")
    db_validator.validate_schema_decomposition(strict=True)


def test_schema_decomposition_rejects_forbidden_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(db_validator, "PROJECT_ROOT", tmp_path)
    write(tmp_path, "services/runtime.py", "from services.schema_tables import build\n")
    with pytest.raises(ValidationError, match="Forbidden import"):
        db_validator.validate_schema_decomposition(strict=True)
    db_validator.validate_schema_decomposition(strict=False)


def test_schema_decomposition_rejects_runtime_ddl_and_migration_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(db_validator, "PROJECT_ROOT", tmp_path)
    write(tmp_path, "services/runtime_ddl.py", "sql = 'CREATE TABLE hidden(id INTEGER)'\n")
    with pytest.raises(ValidationError, match="Forbidden migration/DDL"):
        db_validator.validate_schema_decomposition(strict=True)

    (tmp_path / "services/runtime_ddl.py").unlink()
    write(tmp_path, "services/runtime_migration.py", "sql = 'UPDATE schema_migrations SET version=2'\n")
    with pytest.raises(ValidationError, match="Forbidden migration/DDL"):
        db_validator.validate_schema_decomposition(strict=True)
    db_validator.validate_schema_decomposition(strict=False)


def test_schema_decomposition_skips_unreadable_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(db_validator, "PROJECT_ROOT", tmp_path)
    target = tmp_path / "services" / "unreadable.py"
    write(tmp_path, "services/unreadable.py", "safe = True\n")
    original_read_text = Path.read_text

    def broken_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == target:
            raise OSError("unreadable")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", broken_read_text)
    db_validator.validate_schema_decomposition(strict=True)

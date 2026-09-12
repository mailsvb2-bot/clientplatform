from __future__ import annotations

import importlib
import sqlite3
from typing import Any

import pytest

from services.db import core
from services.db.sql_compat_guard import (
    rewrite_qmark_placeholders,
    validate_sqlite_compat_statement,
)


class RawCursor:
    def __init__(self) -> None:
        self.rowcount = -1
        self.execute_calls: list[tuple[str, Any]] = []
        self.executemany_calls: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any) -> None:
        self.execute_calls.append((sql, params))

    def executemany(self, sql: str, params: Any) -> None:
        self.executemany_calls.append((sql, params))


class RawConnection:
    def __init__(self, cursor: RawCursor) -> None:
        self.raw_cursor = cursor

    def cursor(self) -> RawCursor:
        return self.raw_cursor

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _compat_cursor() -> tuple[Any, RawCursor]:
    raw = RawCursor()
    connection = core.PostgresCompatConnection(RawConnection(raw))
    return core.PostgresCompatCursor(raw, connection), raw


def test_qmark_rewrite_preserves_literals_identifiers_and_comments() -> None:
    sql = (
        "SELECT 'it''s ?' AS text, \"odd\"\"?name\", ? "
        "-- documentation ?\n"
        "FROM demo /* block ? */ WHERE id=?"
    )

    translated, count = rewrite_qmark_placeholders(sql)

    assert count == 2
    assert translated == (
        "SELECT 'it''s ?' AS text, \"odd\"\"?name\", %s "
        "-- documentation ?\n"
        "FROM demo /* block ? */ WHERE id=%s"
    )
    assert core._replace_qmark_placeholders(sql) == translated
    assert core.translate_sql_for_postgres(sql) == translated


def test_driver_rewrite_escapes_original_percent_without_touching_qmark_params() -> None:
    sql = (
        "SELECT ? WHERE note LIKE '%provider_call_started_non_idempotent%' "
        "-- operator % in documentation\n"
        "AND remainder=(10 % 3)"
    )

    lexical, count = rewrite_qmark_placeholders(sql)
    driver_safe = core._replace_qmark_placeholders(sql)

    assert count == 1
    assert lexical.count("%s") == 1
    assert "LIKE '%provider_call_started_non_idempotent%'" in lexical
    assert driver_safe.count("%s") == 1
    assert "LIKE '%%provider_call_started_non_idempotent%%'" in driver_safe
    assert "-- operator %% in documentation" in driver_safe
    assert "remainder=(10 %% 3)" in driver_safe


def test_sqlite_master_counts_only_real_parameters() -> None:
    sql = (
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN (?,?) -- ignored ?\n"
        "/* ignored ? */"
    )

    translated = core._translate_sqlite_master_tables_query(sql)

    assert translated is not None
    assert translated.endswith("table_name IN (%s,%s)")


def test_unsupported_pragma_fails_before_driver_execute() -> None:
    cursor, raw = _compat_cursor()

    with pytest.raises(sqlite3.OperationalError, match="unsupported SQLite PRAGMA.*cache_size"):
        cursor.execute("PRAGMA cache_size")

    assert raw.execute_calls == []


def test_malformed_table_info_fails_before_driver_execute() -> None:
    cursor, raw = _compat_cursor()

    with pytest.raises(sqlite3.OperationalError, match="invalid PRAGMA table_info identifier"):
        cursor.execute("PRAGMA table_info(users;drop)")

    assert raw.execute_calls == []


def test_supported_table_info_reaches_driver_as_information_schema_query() -> None:
    cursor, raw = _compat_cursor()

    cursor.execute('PRAGMA table_info("users")')

    assert len(raw.execute_calls) == 1
    translated, params = raw.execute_calls[0]
    assert "information_schema.columns" in translated
    assert "table_name='users'" in translated
    assert params == ()


def test_executemany_rejects_pragma_before_driver() -> None:
    cursor, raw = _compat_cursor()

    with pytest.raises(sqlite3.OperationalError, match="unsupported SQLite PRAGMA"):
        cursor.executemany("PRAGMA foreign_keys=ON", [()])

    assert raw.executemany_calls == []


def test_guard_validation_and_core_reload_are_stable() -> None:
    validate_sqlite_compat_statement("SELECT 1")
    validate_sqlite_compat_statement("PRAGMA table_info(valid_table)")

    with pytest.raises(sqlite3.OperationalError):
        validate_sqlite_compat_statement("PRAGMA table_info(invalid-name)")

    reloaded = importlib.reload(core)
    sql = "SELECT ? -- ignored ?\n/* ignored ? */"
    assert reloaded.translate_sql_for_postgres(sql) == "SELECT %s -- ignored ?\n/* ignored ? */"

    cursor, raw = _compat_cursor()
    with pytest.raises(sqlite3.OperationalError, match="unsupported SQLite PRAGMA"):
        cursor.execute("PRAGMA cache_size")
    assert raw.execute_calls == []


def test_core_attribute_mutations_reach_function_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "DATABASE_URL", "postgresql://core-test")
    monkeypatch.setattr(core, "POSTGRES_REUSE_CONNECTIONS_TEST_SENTINEL", "ok", raising=False)

    assert core.get_connection.__globals__["DATABASE_URL"] == "postgresql://core-test"
    assert core.POSTGRES_REUSE_CONNECTIONS_TEST_SENTINEL == "ok"

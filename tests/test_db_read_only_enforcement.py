from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from services.db import read_only


def test_sqlite_read_only_context_is_enforced_by_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO sample(value) VALUES('before')")
    conn.commit()

    monkeypatch.setattr(read_only, "get_connection", lambda: conn)
    monkeypatch.setattr(read_only, "is_postgres_enabled", lambda: False)

    with read_only.get_db_ro() as ro:
        assert ro.execute("SELECT value FROM sample").fetchone()[0] == "before"
        with pytest.raises(RuntimeError, match="rejected write SQL"):
            ro.execute("INSERT INTO sample(value) VALUES('blocked')")
        # Bypass the Python wrapper deliberately: SQLite query_only must still
        # reject the underlying write at the database boundary.
        with pytest.raises(sqlite3.OperationalError):
            ro._conn.execute("INSERT INTO sample(value) VALUES('bypass')")
        with pytest.raises(RuntimeError, match="rejected commit"):
            ro.commit()

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_read_only_setup_fails_closed_and_still_cleans_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class BrokenConnection:
        def execute(self, _sql: str, _params: tuple[Any, ...] = ()):
            calls.append("execute")
            raise RuntimeError("cannot enable read-only")

        def rollback(self) -> None:
            calls.append("rollback")

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(read_only, "get_connection", BrokenConnection)
    monkeypatch.setattr(read_only, "is_postgres_enabled", lambda: False)

    with pytest.raises(RuntimeError, match="cannot enable read-only"):
        with read_only.get_db_ro():
            pytest.fail("read-only context must not yield when enforcement fails")

    assert calls == ["execute", "rollback", "close"]


class _Cursor:
    def __init__(self, row: Any):
        self._row = row

    def fetchone(self) -> Any:
        return self._row


class _PostgresConnection:
    def __init__(self, state: str = "on") -> None:
        self.state = state
        self.statements: list[str] = []
        self.rolled_back = False
        self.closed = False

    def execute(self, sql: str, _params: tuple[Any, ...] = ()) -> _Cursor:
        self.statements.append(sql)
        if sql == "SHOW transaction_read_only":
            return _Cursor({"transaction_read_only": self.state})
        return _Cursor(None)

    def cursor(self) -> Any:
        raise AssertionError("cursor is not needed in this test")

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def test_postgres_read_only_transaction_is_enabled_and_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _PostgresConnection("on")
    monkeypatch.setattr(read_only, "get_connection", lambda: conn)
    monkeypatch.setattr(read_only, "is_postgres_enabled", lambda: True)

    with read_only.get_db_ro() as ro:
        ro.execute("SELECT 1")

    assert conn.statements[:2] == ["SET TRANSACTION READ ONLY", "SHOW transaction_read_only"]
    assert conn.statements[-1] == "SELECT 1"
    assert conn.rolled_back and conn.closed


def test_postgres_read_only_verification_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _PostgresConnection("off")
    monkeypatch.setattr(read_only, "get_connection", lambda: conn)
    monkeypatch.setattr(read_only, "is_postgres_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="did not enter read-only mode"):
        with read_only.get_db_ro():
            pytest.fail("read-only context must not yield when server reports off")

    assert conn.rolled_back and conn.closed


def test_ambient_read_only_view_reuses_outer_connection_without_owning_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO sample(value) VALUES('visible')")

    monkeypatch.setattr(read_only, "get_ambient_connection", lambda: conn)
    monkeypatch.setattr(
        read_only,
        "get_connection",
        lambda: pytest.fail("ambient read-only view must not open a second connection"),
    )

    with read_only.get_db_ro() as ro:
        assert ro.execute("SELECT value FROM sample").fetchone()[0] == "visible"
        with pytest.raises(RuntimeError, match="rejected write SQL"):
            ro.execute("INSERT INTO sample(value) VALUES('blocked')")
        with pytest.raises(RuntimeError, match="rejected rollback"):
            ro.rollback()
        ro.close()

    assert conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 1
    conn.rollback()
    conn.close()


def test_public_db_package_exports_enforced_read_only_context() -> None:
    import services.db as db_package

    assert db_package.get_db_ro is read_only.get_db_ro

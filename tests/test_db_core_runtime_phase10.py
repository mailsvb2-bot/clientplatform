from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from services.db import core


class PingConnection:
    def __init__(
        self,
        *,
        closed: bool = False,
        execute_error: BaseException | None = None,
        rollback_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.closed = closed
        self.execute_error = execute_error
        self.rollback_error = rollback_error
        self.close_error = close_error
        self.execute_calls: list[str] = []
        self.rollback_calls = 0
        self.close_calls = 0

    def execute(self, sql: str) -> None:
        self.execute_calls.append(sql)
        if self.execute_error is not None:
            raise self.execute_error

    def rollback(self) -> None:
        self.rollback_calls += 1
        if self.rollback_error is not None:
            raise self.rollback_error

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class PsycopgDouble:
    def __init__(self) -> None:
        self.connections: list[PingConnection] = []
        self.calls: list[tuple[str, bool, Any]] = []

    def connect(self, url: str, *, autocommit: bool, row_factory: Any) -> PingConnection:
        self.calls.append((url, autocommit, row_factory))
        conn = PingConnection()
        self.connections.append(conn)
        return conn


class LifecycleConnection:
    def __init__(
        self,
        *,
        commit_error: BaseException | None = None,
        rollback_error: BaseException | None = None,
        close_error: BaseException | None = None,
        execute_error: BaseException | None = None,
    ) -> None:
        self.commit_error = commit_error
        self.rollback_error = rollback_error
        self.close_error = close_error
        self.execute_error = execute_error
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0
        self.execute_calls: list[str] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        del params
        self.execute_calls.append(sql)
        if self.execute_error is not None:
            raise self.execute_error

    def commit(self) -> None:
        self.commit_calls += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.rollback_calls += 1
        if self.rollback_error is not None:
            raise self.rollback_error

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class ResultCursor:
    def __init__(
        self,
        *,
        rowcount: Any = 0,
        one: Any = None,
        all_rows: list[Any] | None = None,
        description: Any = None,
    ) -> None:
        self.rowcount = rowcount
        self.one = one
        self.all_rows = list(all_rows or [])
        self.description = description

    def fetchone(self) -> Any:
        return self.one

    def fetchall(self) -> list[Any]:
        return list(self.all_rows)


class ExecuteConnection(LifecycleConnection):
    def __init__(self, cursors: list[ResultCursor]) -> None:
        super().__init__()
        self.cursors = list(cursors)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> ResultCursor:
        self.calls.append((sql, params))
        return self.cursors.pop(0)


def install_connection(monkeypatch: pytest.MonkeyPatch, conn: Any, *, postgres: bool) -> None:
    monkeypatch.setattr(core, "get_connection", lambda: conn)
    monkeypatch.setattr(core, "is_postgres_enabled", lambda: postgres)


def test_env_flag_and_connection_max_age(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHASE10_FLAG", raising=False)
    assert core._env_flag("PHASE10_FLAG") is True
    for raw in ("0", "false", "NO", "off"):
        monkeypatch.setenv("PHASE10_FLAG", raw)
        assert core._env_flag("PHASE10_FLAG") is False
    monkeypatch.setenv("PHASE10_FLAG", "yes")
    assert core._env_flag("PHASE10_FLAG") is True

    monkeypatch.delenv("POSTGRES_CONNECTION_MAX_AGE_SEC", raising=False)
    assert core._pg_connection_max_age_sec() == 300.0
    monkeypatch.setenv("POSTGRES_CONNECTION_MAX_AGE_SEC", "12.5")
    assert core._pg_connection_max_age_sec() == 12.5
    monkeypatch.setenv("POSTGRES_CONNECTION_MAX_AGE_SEC", "-1")
    assert core._pg_connection_max_age_sec() == 0.0
    monkeypatch.setenv("POSTGRES_CONNECTION_MAX_AGE_SEC", "bad")
    assert core._pg_connection_max_age_sec() == 300.0


def test_raw_postgres_connection_health_and_safe_close() -> None:
    closed = PingConnection(closed=True)
    assert core._raw_pg_connection_is_usable(closed) is False
    assert closed.execute_calls == []

    healthy = PingConnection()
    assert core._raw_pg_connection_is_usable(healthy) is True
    assert healthy.execute_calls == ["SELECT 1"]
    assert healthy.rollback_calls == 1

    broken = PingConnection(execute_error=RuntimeError("dead"))
    assert core._raw_pg_connection_is_usable(broken) is False

    rollback_broken = PingConnection(rollback_error=RuntimeError("dead"))
    assert core._raw_pg_connection_is_usable(rollback_broken) is False

    core._close_raw_pg_connection(healthy)
    assert healthy.close_calls == 1
    core._close_raw_pg_connection(PingConnection(close_error=RuntimeError("close")))


def test_reusable_postgres_connection_fresh_expired_and_new(monkeypatch: pytest.MonkeyPatch) -> None:
    psycopg = PsycopgDouble()
    row_factory = object()
    local = SimpleNamespace()
    monkeypatch.setattr(core, "_PG_LOCAL", local)
    monkeypatch.setattr(core, "DATABASE_URL", "postgresql://db")
    monkeypatch.setattr(core.time, "monotonic", lambda: 100.0)
    monkeypatch.setenv("POSTGRES_CONNECTION_MAX_AGE_SEC", "30")

    first = core._get_reusable_postgres_connection(psycopg, row_factory)
    assert first is psycopg.connections[0]
    assert local.postgres_connection_created_at == 100.0

    monkeypatch.setattr(core.time, "monotonic", lambda: 110.0)
    reused = core._get_reusable_postgres_connection(psycopg, row_factory)
    assert reused is first
    assert len(psycopg.connections) == 1

    monkeypatch.setattr(core.time, "monotonic", lambda: 150.0)
    replaced = core._get_reusable_postgres_connection(psycopg, row_factory)
    assert replaced is not first
    assert first.close_calls == 1
    assert len(psycopg.connections) == 2

    monkeypatch.setenv("POSTGRES_CONNECTION_MAX_AGE_SEC", "0")
    monkeypatch.setattr(core.time, "monotonic", lambda: 1000.0)
    assert core._get_reusable_postgres_connection(psycopg, row_factory) is replaced


def test_get_connection_postgres_reuse_and_one_shot(monkeypatch: pytest.MonkeyPatch) -> None:
    psycopg = PsycopgDouble()
    row_factory = object()
    monkeypatch.setattr(core, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(core, "_load_psycopg", lambda: (psycopg, row_factory))
    monkeypatch.setattr(core, "DATABASE_URL", "postgresql://db")
    reusable_raw = PingConnection()
    monkeypatch.setattr(core, "_get_reusable_postgres_connection", lambda *_args: reusable_raw)

    monkeypatch.setenv("POSTGRES_REUSE_CONNECTIONS", "1")
    reusable = core.get_connection()
    assert isinstance(reusable, core.PostgresCompatConnection)
    reusable.close()
    assert reusable_raw.close_calls == 0

    monkeypatch.setenv("POSTGRES_REUSE_CONNECTIONS", "0")
    one_shot = core.get_connection()
    assert isinstance(one_shot, core.PostgresCompatConnection)
    one_shot.close()
    assert psycopg.connections[-1].close_calls == 1


def test_get_connection_sqlite_configures_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = tmp_path / "runtime" / "data.db"
    monkeypatch.setattr(core, "is_postgres_enabled", lambda: False)
    monkeypatch.setattr(core, "DB_PATH", db_path)

    conn = core.get_connection()
    try:
        assert db_path.parent.exists()
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
        conn.commit()
    finally:
        conn.close()


def test_write_sql_classification() -> None:
    for sql in (
        "INSERT x",
        " UPDATE x",
        "DELETE x",
        "REPLACE x",
        "CREATE TABLE x(a)",
        "DROP TABLE x",
        "ALTER TABLE x ADD a",
    ):
        assert core._is_write_sql(sql) is True
    assert core._is_write_sql("SELECT 1") is False


def test_get_db_read_only_success_failure_and_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = LifecycleConnection()
    install_connection(monkeypatch, conn, postgres=True)
    with core.get_db_ro() as entered:
        assert entered is conn
    assert conn.rollback_calls == 1
    assert conn.close_calls == 1

    conn = LifecycleConnection()
    install_connection(monkeypatch, conn, postgres=False)
    with core.get_db_ro():
        pass
    assert conn.rollback_calls == 0
    assert conn.close_calls == 1

    conn = LifecycleConnection()
    install_connection(monkeypatch, conn, postgres=True)
    with pytest.raises(ValueError):
        with core.get_db_ro():
            raise ValueError("body")
    assert conn.rollback_calls == 1
    assert conn.close_calls == 1


def test_atomic_db_reuses_one_connection_and_owns_single_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = LifecycleConnection()
    install_connection(monkeypatch, conn, postgres=False)

    with core.atomic_db() as outer:
        with core.get_db() as inner:
            assert inner is outer is conn
        with core.atomic_db() as nested:
            assert nested is conn
        assert conn.commit_calls == 0
        assert conn.close_calls == 0

    assert conn.commit_calls == 1
    assert conn.rollback_calls == 0
    assert conn.close_calls == 1
    assert conn.execute_calls == [
        "BEGIN",
        "SAVEPOINT clientplatform_ambient_1",
        "RELEASE SAVEPOINT clientplatform_ambient_1",
        "SAVEPOINT clientplatform_ambient_2",
        "RELEASE SAVEPOINT clientplatform_ambient_2",
    ]
    assert core.get_ambient_connection() is None


def test_atomic_db_begin_failure_rolls_back_closes_and_never_sets_ambient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = LifecycleConnection(execute_error=RuntimeError("begin failed"))
    install_connection(monkeypatch, conn, postgres=False)

    with pytest.raises(RuntimeError, match="begin failed"):
        with core.atomic_db():
            pytest.fail("atomic_db must not yield after BEGIN failure")

    assert conn.execute_calls == ["BEGIN"]
    assert conn.rollback_calls == 1
    assert conn.close_calls == 1
    assert core.get_ambient_connection() is None


def test_atomic_db_nested_failure_rolls_back_to_savepoint_when_caller_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = LifecycleConnection()
    install_connection(monkeypatch, conn, postgres=False)

    with core.atomic_db():
        try:
            with core.get_db():
                raise ValueError("domain rejection")
        except ValueError:
            pass
        with core.get_db():
            pass

    assert conn.commit_calls == 1
    assert conn.rollback_calls == 0
    assert conn.close_calls == 1
    assert conn.execute_calls == [
        "BEGIN",
        "SAVEPOINT clientplatform_ambient_1",
        "ROLLBACK TO SAVEPOINT clientplatform_ambient_1",
        "RELEASE SAVEPOINT clientplatform_ambient_1",
        "SAVEPOINT clientplatform_ambient_2",
        "RELEASE SAVEPOINT clientplatform_ambient_2",
    ]


def test_nested_atomic_db_failure_uses_savepoint_when_outer_caller_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = LifecycleConnection()
    install_connection(monkeypatch, conn, postgres=False)

    with core.atomic_db():
        try:
            with core.atomic_db():
                raise ValueError("nested atomic rejection")
        except ValueError:
            pass

    assert conn.commit_calls == 1
    assert conn.rollback_calls == 0
    assert conn.close_calls == 1
    assert conn.execute_calls == [
        "BEGIN",
        "SAVEPOINT clientplatform_ambient_1",
        "ROLLBACK TO SAVEPOINT clientplatform_ambient_1",
        "RELEASE SAVEPOINT clientplatform_ambient_1",
    ]


def test_atomic_db_sqlite_savepoint_release_never_commits_outer_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ambient-atomic.db"

    def open_connection() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(core, "get_connection", open_connection)
    monkeypatch.setattr(core, "is_postgres_enabled", lambda: False)

    with core.atomic_db() as outer:
        outer.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        try:
            with core.get_db() as nested:
                nested.execute("INSERT INTO sample(id,value) VALUES(1,'partial')")
                raise ValueError("domain rejection")
        except ValueError:
            pass
        assert outer.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 0
        with core.get_db() as nested:
            nested.execute("INSERT INTO sample(id,value) VALUES(2,'committed')")

    verify = sqlite3.connect(db_path)
    try:
        rows = verify.execute("SELECT id,value FROM sample ORDER BY id").fetchall()
    finally:
        verify.close()
    assert rows == [(2, "committed")]


def test_atomic_db_rolls_back_nested_write_on_later_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = LifecycleConnection()
    install_connection(monkeypatch, conn, postgres=False)

    with pytest.raises(RuntimeError, match="outbox failed"):
        with core.atomic_db():
            with core.get_db() as inner:
                assert inner is conn
            raise RuntimeError("outbox failed")

    assert conn.commit_calls == 0
    assert conn.rollback_calls == 1
    assert conn.close_calls == 1
    assert conn.execute_calls == [
        "BEGIN",
        "SAVEPOINT clientplatform_ambient_1",
        "RELEASE SAVEPOINT clientplatform_ambient_1",
    ]
    assert core.get_ambient_connection() is None


def test_get_db_commit_body_and_cleanup_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = LifecycleConnection()
    install_connection(monkeypatch, conn, postgres=False)
    with core.get_db() as entered:
        assert entered is conn
    assert conn.commit_calls == 1
    assert conn.close_calls == 1

    conn = LifecycleConnection()
    install_connection(monkeypatch, conn, postgres=False)
    with pytest.raises(ValueError):
        with core.get_db():
            raise ValueError("body")
    assert conn.rollback_calls == 1
    assert conn.close_calls == 1

    conn = LifecycleConnection(commit_error=RuntimeError("commit"))
    install_connection(monkeypatch, conn, postgres=False)
    with pytest.raises(RuntimeError, match="commit"):
        with core.get_db():
            pass
    assert conn.rollback_calls == 1
    assert conn.close_calls == 1

    conn = LifecycleConnection(close_error=RuntimeError("close"))
    install_connection(monkeypatch, conn, postgres=False)
    with core.get_db():
        pass
    assert conn.close_calls == 1


def test_db_alias_tx_write_and_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ExecuteConnection(
        [
            ResultCursor(rowcount=2),
            ResultCursor(rowcount=None),
            ResultCursor(one={"x": 1}),
            ResultCursor(all_rows=[{"x": 1}], description=("x",)),
            ResultCursor(all_rows=[{"x": 2}], description=("x",)),
            ResultCursor(rowcount=3),
            ResultCursor(rowcount="bad"),
        ]
    )

    @contextmanager
    def fake_db() -> Iterator[ExecuteConnection]:
        yield conn

    monkeypatch.setattr(core, "db", fake_db)
    monkeypatch.setattr(core, "is_postgres_enabled", lambda: False)

    with core.tx(conn) as entered:
        assert entered is conn

    assert core.write("UPDATE x SET a=?", (1,)) == 2
    assert core.write("UPDATE x SET a=?", (2,)) == 0
    assert core.execute("SELECT 1", fetchone=True) == {"x": 1}
    assert core.execute("SELECT 1", fetchall=True) == [{"x": 1}]
    assert core.execute("SELECT 1") == [{"x": 2}]
    assert core.execute("UPDATE x SET a=1") == 3
    assert core.execute("UPDATE x SET a=1") == 0

    with pytest.raises(ValueError, match="only one"):
        core.execute("SELECT 1", fetchone=True, fetchall=True)


def test_write_postgres_rowcount(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ExecuteConnection([ResultCursor(rowcount=4)])

    @contextmanager
    def fake_db() -> Iterator[ExecuteConnection]:
        yield conn

    monkeypatch.setattr(core, "db", fake_db)
    monkeypatch.setattr(core, "is_postgres_enabled", lambda: True)
    assert core.write("UPDATE x SET a=1") == 4


def test_delivery_key_and_deferred_marker() -> None:
    assert core._delivery_key("a", " ", 2) == "a:2"
    with pytest.raises(ValueError, match="must not be empty"):
        core._delivery_key("", " ")
    assert core._is_deferred_engine_job_marker("job", "user", "kind") is True
    assert core._is_deferred_engine_job_marker("job", "user") is False
    assert core._is_deferred_engine_job_marker("delivery", "user", "kind") is False


def test_delivery_idempotency_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        ResultCursor(one={"exists": 1}),
        ResultCursor(),
        ResultCursor(one={"c": 1}),
        ResultCursor(),
    ]
    conn = ExecuteConnection(rows)

    @contextmanager
    def fake_db() -> Iterator[ExecuteConnection]:
        yield conn

    monkeypatch.setattr(core, "db", fake_db)
    monkeypatch.setattr(core.time, "time", lambda: 123)

    assert core.was_delivered(7, "demo", 1) is True
    assert core.mark_delivery_once(7, "demo", 1) is True
    core.unmark_delivery(7, "demo", 1)
    assert conn.calls[0][1] == (7, "demo:1")
    assert conn.calls[1][1] == (7, "demo:1", 123)
    assert conn.calls[-1][1] == (7, "demo:1")

    before = len(conn.calls)
    assert core.mark_delivery_once(7, "job", "kind", "id") is True
    assert len(conn.calls) == before

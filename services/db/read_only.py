from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Literal, Sequence

from services.db.core import ambient_savepoint, get_ambient_connection, get_connection
from services.db.runtime import is_postgres_enabled

log = logging.getLogger(__name__)

_WRITE_PREFIXES = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "REPLACE",
    "CREATE",
    "DROP",
    "ALTER",
    "TRUNCATE",
    "VACUUM",
    "ATTACH",
    "DETACH",
    "GRANT",
    "REVOKE",
    "COMMENT",
    "COPY",
    "MERGE",
)
_TRANSACTION_PREFIXES = ("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE")


def _normalized_sql(sql: str) -> str:
    return (sql or "").lstrip().upper()


def _is_write_sql(sql: str) -> bool:
    statement = _normalized_sql(sql)
    if statement.startswith(_WRITE_PREFIXES) or statement.startswith(_TRANSACTION_PREFIXES):
        return True
    if statement.startswith("PRAGMA") and "QUERY_ONLY" not in statement:
        return True
    if statement.startswith("SET ") and not statement.startswith("SET TRANSACTION READ ONLY"):
        return True
    return False


def _scalar(row: Any, *keys: str) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        for key in keys:
            if key in row:
                return row[key]
        return next(iter(row.values()), None)
    if hasattr(row, "keys"):
        for key in keys:
            try:
                return row[key]
            except (KeyError, TypeError, IndexError):
                continue
    try:
        return row[0]
    except (TypeError, KeyError, IndexError):
        return row


class ReadOnlyCursor:
    def __init__(self, cursor: Any):
        self._cursor = cursor
        self.rowcount = getattr(cursor, "rowcount", -1)

    def execute(self, sql: str, params: Sequence[Any] = ()):
        if _is_write_sql(sql):
            raise RuntimeError("read-only DB context rejected write SQL")
        result = self._cursor.execute(sql, params)
        self.rowcount = getattr(self._cursor, "rowcount", -1)
        return self if result is self._cursor else result

    def executemany(self, sql: str, params: Sequence[Sequence[Any]]):
        if _is_write_sql(sql):
            raise RuntimeError("read-only DB context rejected write SQL")
        result = self._cursor.executemany(sql, params)
        self.rowcount = getattr(self._cursor, "rowcount", -1)
        return self if result is self._cursor else result

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self) -> None:
        self._cursor.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> Literal[False]:
        self.close()
        return False

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class ReadOnlyConnection:
    def __init__(self, conn: Any, *, owns_connection: bool = True):
        self._conn = conn
        self._owns_connection = bool(owns_connection)

    def execute(self, sql: str, params: Sequence[Any] = ()):
        if _is_write_sql(sql):
            raise RuntimeError("read-only DB context rejected write SQL")
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, params: Sequence[Sequence[Any]]):
        if _is_write_sql(sql):
            raise RuntimeError("read-only DB context rejected write SQL")
        return self._conn.executemany(sql, params)

    def executescript(self, _sql: str):
        raise RuntimeError("read-only DB context rejected SQL script execution")

    def cursor(self):
        return ReadOnlyCursor(self._conn.cursor())

    def commit(self) -> None:
        raise RuntimeError("read-only DB context rejected commit")

    def rollback(self) -> None:
        if self._owns_connection:
            self._conn.rollback()
            return
        raise RuntimeError("ambient read-only DB view rejected rollback")

    def close(self) -> None:
        if self._owns_connection:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> Literal[False]:
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _enable_database_read_only(conn: Any) -> None:
    if is_postgres_enabled():
        conn.execute("SET TRANSACTION READ ONLY")
        row = conn.execute("SHOW transaction_read_only").fetchone()
        state = str(_scalar(row, "transaction_read_only") or "").strip().lower()
        if state not in {"on", "true", "1", "yes"}:
            raise RuntimeError("postgres transaction did not enter read-only mode")
        return

    conn.execute("PRAGMA query_only=ON")
    row = conn.execute("PRAGMA query_only").fetchone()
    try:
        enabled = int(_scalar(row, "query_only") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("sqlite query_only state is invalid") from exc
    if enabled != 1:
        raise RuntimeError("sqlite connection did not enter query-only mode")


@contextmanager
def get_db_ro() -> Iterator[ReadOnlyConnection]:
    """Open a read-only view and fail closed.

    Normally PostgreSQL/SQLite database-level read-only enforcement is enabled
    and verified. Inside an explicit ``atomic_db()`` write transaction we must
    reuse that exact connection so reads observe uncommitted application writes;
    in that narrow case the Python read-only wrapper remains the guardrail and
    transaction lifecycle stays with the outer atomic scope.
    """

    ambient = get_ambient_connection()
    if ambient is not None:
        with ambient_savepoint(ambient):
            yield ReadOnlyConnection(ambient, owns_connection=False)
        return

    conn = get_connection()
    try:
        _enable_database_read_only(conn)
        yield ReadOnlyConnection(conn)
    finally:
        try:
            conn.rollback()
        except Exception:  # validator: allow-wide-except
            log.exception("read-only rollback failed")
        try:
            conn.close()
        except Exception:  # validator: allow-wide-except
            log.exception("DB close failed")

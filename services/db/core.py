from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Sequence

from services.db.runtime import CONFIG, is_postgres_enabled, postgres_driver_error_hint
from services.db.sql_compat_guard import (
    count_qmark_placeholders,
    replace_qmark_placeholders,
    validate_sqlite_compat_statement,
)

log = logging.getLogger(__name__)

try:
    from core.paths import ROOT as PROJECT_ROOT, DB_PATH, DATABASE_URL
except ImportError:  # pragma: no cover
    PROJECT_ROOT = Path(__file__).resolve().parents[2]
    DB_PATH = PROJECT_ROOT / "data.db"
    DATABASE_URL = ""


class DatabaseOperationDeadlineExceeded(TimeoutError):
    """A caller-owned database operation exhausted its absolute time budget."""


_DB_OPERATION_DEADLINE: ContextVar[float | None] = ContextVar(
    "db_operation_deadline",
    default=None,
)
_DB_AMBIENT_CONNECTION: ContextVar[Any | None] = ContextVar(
    "db_ambient_connection",
    default=None,
)
_DB_AMBIENT_SAVEPOINT_SEQUENCE: ContextVar[int] = ContextVar(
    "db_ambient_savepoint_sequence",
    default=0,
)


@contextmanager
def db_operation_deadline(timeout_seconds: float) -> Iterator[None]:
    """Bound all DB work in this context to one absolute monotonic deadline.

    ``asyncio.to_thread`` copies ContextVars, so async entry points can establish
    a budget once and every repository call executed in worker threads inherits
    the same deadline. Nested callers may only shorten an existing budget.
    """

    timeout = float(timeout_seconds)
    if timeout <= 0:
        raise ValueError("database operation deadline must be positive")
    requested = time.monotonic() + timeout
    existing = _DB_OPERATION_DEADLINE.get()
    deadline = requested if existing is None else min(existing, requested)
    token = _DB_OPERATION_DEADLINE.set(deadline)
    try:
        yield
    finally:
        _DB_OPERATION_DEADLINE.reset(token)


def _remaining_operation_ms(deadline: float | None = None) -> int | None:
    target = _DB_OPERATION_DEADLINE.get() if deadline is None else deadline
    if target is None:
        return None
    remaining = target - time.monotonic()
    if remaining <= 0:
        raise DatabaseOperationDeadlineExceeded("database_operation_deadline_exceeded")
    return max(1, int(remaining * 1000))


def _raise_sqlite_compat(exc: Exception):
    msg = str(exc)
    if isinstance(exc, sqlite3.Error):
        raise exc
    sqlstate = str(getattr(exc, "sqlstate", "") or "")
    if _DB_OPERATION_DEADLINE.get() is not None and sqlstate in {"57014", "55P03"}:
        raise DatabaseOperationDeadlineExceeded(
            "database_operation_deadline_exceeded"
        ) from exc
    text = msg.lower()
    if 'does not exist' in text or 'undefined table' in text or 'undefined column' in text:
        raise sqlite3.OperationalError(msg) from exc
    if 'duplicate key' in text or 'unique constraint' in text:
        raise sqlite3.IntegrityError(msg) from exc
    if 'syntax error' in text or 'invalid input syntax' in text:
        raise sqlite3.OperationalError(msg) from exc
    raise sqlite3.DatabaseError(msg) from exc


class PgRow(dict):
    """Small compatibility shim that behaves close enough to sqlite3.Row."""

    def __getitem__(self, key):
        if isinstance(key, int):
            try:
                return list(self.values())[key]
            except IndexError as exc:
                raise KeyError(key) from exc
        return super().__getitem__(key)


def _is_select_changes_sql(sql: str) -> bool:
    """SQLite compatibility: SELECT changes() [AS c].

    Some legacy service code uses SQLite's changes() function after INSERT OR
    IGNORE / UPDATE to detect whether exactly one row was changed. Postgres has
    no such SQL function; the canonical DB compatibility layer owns this bridge.
    """
    return bool(re.match(r"(?is)^\s*SELECT\s+changes\s*\(\s*\)\s*(?:AS\s+\w+)?\s*$", sql or ""))


def _is_dml_sql(sql: str) -> bool:
    s = (sql or "").lstrip().upper()
    return s.startswith("INSERT") or s.startswith("UPDATE") or s.startswith("DELETE") or s.startswith("REPLACE")


def _is_transaction_control_sql(sql: str) -> bool:
    token = (sql or "").lstrip().split(maxsplit=1)
    return bool(token and token[0].upper() in {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"})


class PostgresCompatCursor:
    def __init__(self, cursor, conn: "PostgresCompatConnection"):
        self._cursor = cursor
        self._conn = conn
        self._synthetic_rows: list[PgRow] | None = None
        self.rowcount = -1

    def execute(self, sql: str, params: Sequence[Any] = ()):
        validate_sqlite_compat_statement(sql)
        if _is_select_changes_sql(sql):
            self._synthetic_rows = [PgRow({"c": int(getattr(self._conn, "last_rowcount", 0) or 0)})]
            self.rowcount = 1
            return self

        self._synthetic_rows = None
        translated = translate_sql_for_postgres(sql)
        try:
            if not _is_transaction_control_sql(translated):
                self._conn._apply_operation_deadline(self._cursor)
            self._cursor.execute(translated, _normalize_params(params))
        except DatabaseOperationDeadlineExceeded:
            raise
        except Exception as exc:  # validator: allow-wide-except
            _raise_sqlite_compat(exc)
        self.rowcount = getattr(self._cursor, "rowcount", -1)
        if _is_dml_sql(sql):
            self._conn.last_rowcount = max(int(self.rowcount or 0), 0)
        return self

    def executemany(self, sql: str, seq_of_params):
        validate_sqlite_compat_statement(sql)
        if self._conn.operation_deadline is not None:
            total = 0
            for params in seq_of_params:
                self.execute(sql, params)
                total += max(int(self.rowcount or 0), 0)
            self.rowcount = total
            if _is_dml_sql(sql):
                self._conn.last_rowcount = total
            return self

        self._synthetic_rows = None
        translated = translate_sql_for_postgres(sql)
        try:
            self._cursor.executemany(translated, [_normalize_params(p) for p in seq_of_params])
        except Exception as exc:  # validator: allow-wide-except
            _raise_sqlite_compat(exc)
        self.rowcount = getattr(self._cursor, "rowcount", -1)
        if _is_dml_sql(sql):
            self._conn.last_rowcount = max(int(self.rowcount or 0), 0)
        return self

    def fetchone(self):
        if self._synthetic_rows is not None:
            if not self._synthetic_rows:
                return None
            return self._synthetic_rows.pop(0)
        try:
            row = self._cursor.fetchone()
        except Exception as exc:  # validator: allow-wide-except
            _raise_sqlite_compat(exc)
        return _wrap_pg_row(row)

    def fetchall(self):
        if self._synthetic_rows is not None:
            rows = self._synthetic_rows
            self._synthetic_rows = []
            return rows
        try:
            rows = self._cursor.fetchall()
        except Exception as exc:  # validator: allow-wide-except
            _raise_sqlite_compat(exc)
        return [_wrap_pg_row(r) for r in rows]

    def close(self):
        self._cursor.close()


class PostgresCompatConnection:
    def __init__(
        self,
        conn,
        *,
        reusable: bool = False,
        operation_deadline: float | None = None,
    ):
        self._conn = conn
        self._reusable = bool(reusable)
        self._operation_deadline = operation_deadline
        self.last_rowcount = 0

    @property
    def operation_deadline(self) -> float | None:
        return self._operation_deadline

    def _apply_operation_deadline(self, cursor: Any) -> None:
        remaining_ms = _remaining_operation_ms(self._operation_deadline)
        if remaining_ms is None:
            return
        lock_timeout_ms = min(int(CONFIG.lock_timeout_ms), remaining_ms)
        cursor.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (f"{remaining_ms}ms",),
        )
        cursor.execute(
            "SELECT set_config('lock_timeout', %s, true)",
            (f"{max(1, lock_timeout_ms)}ms",),
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            try:
                self.commit()
            except Exception as commit_exc:  # validator: allow-wide-except
                try:
                    self.rollback()
                except Exception:  # validator: allow-wide-except
                    logging.getLogger(__name__).exception("Postgres rollback failed after commit error")
                logging.getLogger(__name__).exception("Postgres commit failed on context exit")
                self.close()
                raise commit_exc
        else:
            try:
                self.rollback()
            except Exception:  # validator: allow-wide-except
                logging.getLogger(__name__).exception("Postgres rollback failed on context exit")
        self.close()
        return False

    def cursor(self):
        return PostgresCompatCursor(self._conn.cursor(), self)

    def execute(self, sql: str, params: Sequence[Any] = ()):
        cur = self.cursor()
        return cur.execute(sql, params)

    def commit(self):
        if self._operation_deadline is not None:
            cursor = self._conn.cursor()
            try:
                self._apply_operation_deadline(cursor)
            finally:
                cursor.close()
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()
        self.last_rowcount = 0

    def close(self):
        # For production Postgres we keep one connection per worker thread.
        # This removes the observed connect/auth/close storm while preserving
        # the existing sqlite-like context manager API. Transaction boundaries
        # are still owned by get_db()/get_db_ro() through commit/rollback.
        if self._reusable:
            return
        self._conn.close()

    def force_close(self):
        self._conn.close()


def _wrap_pg_row(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return PgRow(row)
    return row


def _normalize_params(params: Sequence[Any] | None):
    if params is None:
        return ()
    if isinstance(params, (list, tuple)):
        return tuple(params)
    return params


def _replace_qmark_placeholders(sql: str) -> str:
    return replace_qmark_placeholders(sql)


def _translate_insert_or_ignore(sql: str) -> str:
    m = re.match(r"(?is)^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+([\w_]+)\s*(\([^)]*\))\s*(VALUES\s*\(.*\))\s*$", sql.strip())
    if not m:
        return sql
    table, cols, values = m.groups()
    return f"INSERT INTO {table}{cols} {values} ON CONFLICT DO NOTHING"


def _translate_insert_or_replace(sql: str) -> str:
    stripped = sql.strip()
    low = stripped.lower()
    if low.startswith('insert or replace into audio_cache'):
        return _replace_qmark_placeholders(
            "INSERT INTO audio_cache(path, kind, file_id, updated_at_utc) VALUES(?,?,?,?) "
            "ON CONFLICT (path, kind) DO UPDATE SET "
            "file_id=EXCLUDED.file_id, updated_at_utc=EXCLUDED.updated_at_utc"
        )
    if low.startswith('insert or replace into schema_migrations'):
        return _replace_qmark_placeholders(
            "INSERT INTO schema_migrations(name, applied_at_utc) VALUES(?, CURRENT_TIMESTAMP) "
            "ON CONFLICT (name) DO UPDATE SET applied_at_utc=EXCLUDED.applied_at_utc"
        )
    return sql


def _translate_sqlite_master_tables_query(s: str) -> str | None:
    if not re.match(r"(?is)^SELECT\s+(?:name|1)\s+FROM\s+sqlite_master\s+WHERE\s+type='table'", s):
        return None

    base = (
        "SELECT table_name AS name FROM information_schema.tables "
        "WHERE table_schema=current_schema() AND table_type='BASE TABLE'"
    )

    if re.search(r"(?is)\bname\s+IN\s*\(", s):
        placeholder_count = count_qmark_placeholders(s)
        if placeholder_count > 0:
            placeholders = ",".join("%s" for _ in range(placeholder_count))
            return f"{base} AND table_name IN ({placeholders})"

    if re.search(r"(?is)\bname\s*=\s*\?", s):
        return f"{base} AND table_name=%s LIMIT 1"

    if re.search(r"(?is)\bname\s+NOT\s+LIKE\s+'sqlite_%'", s):
        return f"{base} AND table_name NOT LIKE 'sqlite_%'"

    return base


def _sql_string_literal(value: str) -> str:
    """Return a safely quoted SQL string literal for internal SQL translation.

    Used only for SQLite compatibility SQL generated by trusted project code,
    after strict identifier validation.
    """
    return "'" + str(value).replace("'", "''") + "'"


def translate_sql_for_postgres(sql: str) -> str:
    s = (sql or '').strip()
    if not s:
        return sql

    if re.match(r"(?is)^BEGIN\s+IMMEDIATE\s*$", s):
        return 'BEGIN'

    pragma_match = re.match(r"(?is)^PRAGMA\s+table_info\(([^)]+)\)\s*$", s)
    if pragma_match:
        table = pragma_match.group(1).strip().strip('"`[]')
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table):
            return "SELECT 1 WHERE FALSE"
        query = (
            "SELECT ordinal_position - 1 AS cid, column_name AS name, data_type AS type, "
            "CASE WHEN is_nullable='NO' THEN 1 ELSE 0 END AS notnull, column_default AS dflt_value, "
            "CASE WHEN position('nextval' in coalesce(column_default, '')) > 0 THEN 1 ELSE 0 END AS pk "
            "FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=__TABLE_NAME__ "
            "ORDER BY ordinal_position"
        )
        return query.replace("__TABLE_NAME__", _sql_string_literal(table))

    sqlite_master_tables_query = _translate_sqlite_master_tables_query(s)
    if sqlite_master_tables_query is not None:
        return sqlite_master_tables_query

    if s.upper().startswith('PRAGMA '):
        return 'SELECT 1'
    if s.lower() == 'select last_insert_rowid() as id':
        return 'SELECT LASTVAL() AS id'

    s = _translate_insert_or_replace(s)
    s = _translate_insert_or_ignore(s)
    s = s.replace("datetime('now')", 'CURRENT_TIMESTAMP')
    s = s.replace("strftime('%s','now')", 'EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)::BIGINT')

    # DDL compatibility
    # Telegram user/chat identifiers may use up to 52 significant bits. Keep
    # identity-shaped columns 64-bit even when the SQLite source DDL says
    # INTEGER (which is already dynamically wide in SQLite).
    if re.match(r"(?is)^(?:CREATE|ALTER)\s+TABLE\b", s):
        s = re.sub(
            r"\b(?P<name>(?:[A-Za-z_][A-Za-z0-9_]*_)?(?:user_id|chat_id)|admin_id|requested_by)\s+(?:INTEGER|INT)\b",
            lambda match: f"{match.group('name')} BIGINT",
            s,
            flags=re.IGNORECASE,
        )
    # SQLite INTEGER PRIMARY KEY behaves like an auto-generated rowid, so in Postgres
    # it must map to BIGSERIAL PRIMARY KEY rather than plain BIGINT PRIMARY KEY.
    s = re.sub(r'\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b', 'BIGSERIAL PRIMARY KEY', s, flags=re.IGNORECASE)
    s = re.sub(r'\bINT\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b', 'BIGSERIAL PRIMARY KEY', s, flags=re.IGNORECASE)
    s = re.sub(r'\bINTEGER\s+PRIMARY\s+KEY\b', 'BIGSERIAL PRIMARY KEY', s, flags=re.IGNORECASE)
    s = re.sub(r'\bINT\s+PRIMARY\s+KEY\b', 'BIGSERIAL PRIMARY KEY', s, flags=re.IGNORECASE)
    s = re.sub(r'\bAUTOINCREMENT\b', '', s, flags=re.IGNORECASE)

    s = _replace_qmark_placeholders(s)
    return s


def _load_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg, dict_row
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(postgres_driver_error_hint()) from exc


_PG_LOCAL = threading.local()


def _env_flag(name: str, default: str = "1") -> bool:
    raw = (os.getenv(name, default) or default).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _pg_connection_max_age_sec() -> float:
    raw = (os.getenv("POSTGRES_CONNECTION_MAX_AGE_SEC") or "300").strip()
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 300.0


def _raw_pg_connection_is_usable(conn: Any) -> bool:
    """Prove that a cached Postgres connection is alive before reuse.

    PostgreSQL, a proxy or the network can sever an idle socket while psycopg
    still reports ``closed == False``. A lightweight pre-ping plus rollback
    prevents the next business operation from inheriting a dead or ping-opened
    transaction.
    """
    try:
        if bool(getattr(conn, "closed", False)):
            return False
        conn.execute("SELECT 1")
        conn.rollback()
        return True
    except Exception:  # validator: allow-wide-except
        return False


def _close_raw_pg_connection(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # validator: allow-wide-except
        log.debug("Postgres reusable connection close failed", exc_info=True)


def _get_reusable_postgres_connection(psycopg: Any, dict_row: Any) -> Any:
    max_age = _pg_connection_max_age_sec()
    now = time.monotonic()
    cached = getattr(_PG_LOCAL, "postgres_connection", None)
    created = float(getattr(_PG_LOCAL, "postgres_connection_created_at", 0.0) or 0.0)

    if cached is not None and _raw_pg_connection_is_usable(cached):
        if max_age <= 0 or (now - created) <= max_age:
            return cached
        _close_raw_pg_connection(cached)

    conn = psycopg.connect(DATABASE_URL, autocommit=False, row_factory=dict_row)
    _PG_LOCAL.postgres_connection = conn
    _PG_LOCAL.postgres_connection_created_at = now
    return conn


def _operation_pg_connect_kwargs(deadline: float) -> dict[str, Any]:
    remaining_ms = _remaining_operation_ms(deadline)
    assert remaining_ms is not None
    connect_timeout = max(
        1,
        min(
            int(CONFIG.connect_timeout_sec),
            max(1, (remaining_ms + 999) // 1000),
        ),
    )
    lock_timeout_ms = max(1, min(int(CONFIG.lock_timeout_ms), remaining_ms))
    inherited_options = (os.getenv("PGOPTIONS") or "").strip()
    deadline_options = (
        f"-c statement_timeout={remaining_ms}ms "
        f"-c lock_timeout={lock_timeout_ms}ms"
    )
    return {
        "connect_timeout": connect_timeout,
        "options": f"{inherited_options} {deadline_options}".strip(),
    }


def get_connection():
    operation_deadline = _DB_OPERATION_DEADLINE.get()
    if is_postgres_enabled():
        psycopg, dict_row = _load_psycopg()
        if operation_deadline is not None:
            conn = psycopg.connect(
                DATABASE_URL,
                autocommit=False,
                row_factory=dict_row,
                **_operation_pg_connect_kwargs(operation_deadline),
            )
            return PostgresCompatConnection(
                conn,
                operation_deadline=operation_deadline,
            )
        if _env_flag("POSTGRES_REUSE_CONNECTIONS", "1"):
            conn = _get_reusable_postgres_connection(psycopg, dict_row)
            return PostgresCompatConnection(conn, reusable=True)
        conn = psycopg.connect(DATABASE_URL, autocommit=False, row_factory=dict_row)
        return PostgresCompatConnection(conn)

    try:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    sqlite_timeout = 30.0
    remaining_ms = _remaining_operation_ms(operation_deadline)
    if remaining_ms is not None:
        sqlite_timeout = max(0.001, min(sqlite_timeout, remaining_ms / 1000.0))
    conn = sqlite3.connect(DB_PATH, timeout=sqlite_timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if operation_deadline is not None:
        def _abort_expired_sqlite_query() -> int:
            return int(time.monotonic() >= operation_deadline)

        conn.set_progress_handler(_abort_expired_sqlite_query, 1000)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        busy_timeout_ms = 10000 if remaining_ms is None else min(10000, remaining_ms)
        conn.execute(f"PRAGMA busy_timeout={max(1, int(busy_timeout_ms))};")
        conn.execute("PRAGMA foreign_keys=ON;")
    except sqlite3.Error as e:
        log.warning("PRAGMA init failed: %s", e)
    return conn


def get_ambient_connection() -> Any | None:
    """Return the caller-owned ambient write transaction, if one exists."""

    return _DB_AMBIENT_CONNECTION.get()


def _next_ambient_savepoint_name() -> str:
    sequence = _DB_AMBIENT_SAVEPOINT_SEQUENCE.get() + 1
    _DB_AMBIENT_SAVEPOINT_SEQUENCE.set(sequence)
    return f"clientplatform_ambient_{sequence}"


@contextmanager
def ambient_savepoint(conn: Any) -> Iterator[Any]:
    """Preserve the historical rollback boundary of one nested DB scope.

    An exception may be intentionally handled by a higher application layer.
    Rolling back to a savepoint keeps partial writes from that failed nested
    operation out of the surrounding ``atomic_db()`` transaction while still
    allowing the outer scope to persist a safe/stale/permission reply.
    """

    name = _next_ambient_savepoint_name()
    conn.execute(f"SAVEPOINT {name}")  # nosec B608 - generated internal identifier only
    try:
        yield conn
    except Exception:
        try:
            conn.execute(
                f"ROLLBACK TO SAVEPOINT {name}"  # nosec B608 - generated internal identifier only
            )
        finally:
            conn.execute(
                f"RELEASE SAVEPOINT {name}"  # nosec B608 - generated internal identifier only
            )
        raise
    else:
        conn.execute(
            f"RELEASE SAVEPOINT {name}"  # nosec B608 - generated internal identifier only
        )


@contextmanager
def atomic_db() -> Iterator[Any]:
    """Share one commit/rollback boundary across nested application DB calls.

    Nested ``get_db()`` calls reuse this connection without committing or closing
    it. Read-only application calls receive a guarded view of the same connection
    from :mod:`services.db.read_only`, so they can observe earlier writes in the
    transaction without opening a second snapshot. Only the outermost scope owns
    commit, rollback and connection lifecycle.
    """

    existing = _DB_AMBIENT_CONNECTION.get()
    if existing is not None:
        with ambient_savepoint(existing):
            yield existing
        return

    conn = get_connection()
    # SQLite treats a top-level SAVEPOINT as the transaction itself, so
    # releasing that savepoint would commit before the ambient unit of work
    # reaches its durable reply. Establish the outer transaction explicitly
    # on every engine before nested application scopes create savepoints.
    try:
        conn.execute("BEGIN")
    except Exception:
        try:
            conn.rollback()
        except Exception:  # validator: allow-wide-except
            logging.getLogger(__name__).exception("Ambient DB rollback after BEGIN failure failed")
        try:
            conn.close()
        except Exception:  # validator: allow-wide-except
            logging.getLogger(__name__).exception("Ambient DB close after BEGIN failure failed")
        raise
    token = _DB_AMBIENT_CONNECTION.set(conn)
    sequence_token = _DB_AMBIENT_SAVEPOINT_SEQUENCE.set(0)
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:  # validator: allow-wide-except
            logging.getLogger(__name__).exception("Ambient DB rollback after body failure failed")
        raise
    else:
        try:
            conn.commit()
        except Exception:  # validator: allow-wide-except
            logging.getLogger(__name__).exception("Ambient DB commit failed")
            try:
                conn.rollback()
            except Exception:  # validator: allow-wide-except
                logging.getLogger(__name__).exception("Ambient DB rollback after commit failure failed")
            raise
    finally:
        _DB_AMBIENT_SAVEPOINT_SEQUENCE.reset(sequence_token)
        _DB_AMBIENT_CONNECTION.reset(token)
        try:
            conn.close()
        except Exception:  # validator: allow-wide-except
            logging.getLogger(__name__).exception("Ambient DB close failed")


def _is_write_sql(sql: str) -> bool:
    s = (sql or "").lstrip().upper()
    return (
        s.startswith("INSERT")
        or s.startswith("UPDATE")
        or s.startswith("DELETE")
        or s.startswith("REPLACE")
        or s.startswith("CREATE")
        or s.startswith("DROP")
        or s.startswith("ALTER")
    )


@contextmanager
def get_db_ro() -> Iterator[Any]:
    conn = get_connection()
    try:
        yield conn
        if is_postgres_enabled():
            try:
                conn.rollback()
            except Exception:  # validator: allow-wide-except
                logging.getLogger(__name__).exception("DB read-only rollback failed")
    except Exception:
        if is_postgres_enabled():
            try:
                conn.rollback()
            except Exception:  # validator: allow-wide-except
                logging.getLogger(__name__).exception("DB read-only rollback after failure failed")
        raise
    finally:
        try:
            conn.close()
        except Exception:  # validator: allow-wide-except
            logging.getLogger(__name__).exception("DB close failed")


@contextmanager
def get_db() -> Iterator[Any]:
    ambient = get_ambient_connection()
    if ambient is not None:
        with ambient_savepoint(ambient):
            yield ambient
        return

    conn = get_connection()
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:  # validator: allow-wide-except
            logging.getLogger(__name__).exception("DB rollback after body failure failed")
        raise
    else:
        try:
            conn.commit()
        except Exception:  # validator: allow-wide-except
            logging.getLogger(__name__).exception("DB commit failed")
            try:
                conn.rollback()
            except Exception:  # validator: allow-wide-except
                logging.getLogger(__name__).exception("DB rollback after commit failure failed")
            raise
    finally:
        try:
            conn.close()
        except Exception:  # validator: allow-wide-except
            logging.getLogger(__name__).exception("DB close failed")


@contextmanager
def db() -> Iterator[Any]:
    with get_db() as conn:
        yield conn


def write(sql: str, params: tuple[Any, ...] = ()) -> int:
    with db() as conn:
        cur = conn.execute(sql, params)
        if is_postgres_enabled():
            return int(getattr(cur, 'rowcount', 0) or 0)
        try:
            return int(cur.rowcount)
        except (AttributeError, TypeError):
            return 0


def execute(
    sql: str,
    params: Sequence[Any] = (),
    *,
    fetchone: bool = False,
    fetchall: bool = False,
):
    """Execute one statement and materialize results before close."""
    if fetchone and fetchall:
        raise ValueError("execute accepts only one of fetchone/fetchall")
    with db() as conn:
        cursor = conn.execute(sql, tuple(params))
        if fetchone:
            return cursor.fetchone()
        if fetchall:
            return cursor.fetchall()
        if getattr(cursor, "description", None) is not None:
            return cursor.fetchall()
        try:
            return int(getattr(cursor, "rowcount", 0) or 0)
        except (TypeError, ValueError):
            return 0


@contextmanager
def tx(conn):
    """Nested transaction scope that never owns connection lifecycle.

    Historically callers used `with db() as conn: with tx(conn): ...` while tx()
    returned the raw sqlite connection. SQLite's connection context manager commits
    but does not close, so this pattern survived. PostgresCompatConnection closes
    in __exit__, therefore returning conn from tx() closes the outer connection
    before get_db() can commit. Commit/rollback/close ownership belongs to get_db().
    """
    yield conn


def _delivery_key(*parts: Any) -> str:
    cleaned = [str(part).strip() for part in parts if str(part).strip()]
    if not cleaned:
        raise ValueError("delivery idempotency key must not be empty")
    return ":".join(cleaned)


def _is_deferred_engine_job_marker(*parts: Any) -> bool:
    return len(parts) >= 3 and str(parts[0]).strip() == "job"


def was_delivered(user_id: int, *key_parts: Any) -> bool:
    key = _delivery_key(*key_parts)
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM idempotency WHERE user_id=? AND key=? LIMIT 1",
            (int(user_id), key),
        ).fetchone()
        return bool(row)


def mark_delivery_once(user_id: int, *key_parts: Any) -> bool:
    key = _delivery_key(*key_parts)
    if _is_deferred_engine_job_marker(*key_parts):
        return True
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO idempotency(user_id, key, created_at) VALUES(?,?,?)",
            (int(user_id), key, int(time.time())),
        )
        row = conn.execute("SELECT changes() AS c").fetchone()
        return int(row["c"] if hasattr(row, "keys") else row[0]) == 1


def unmark_delivery(user_id: int, *key_parts: Any) -> None:
    key = _delivery_key(*key_parts)
    with db() as conn:
        conn.execute("DELETE FROM idempotency WHERE user_id=? AND key=?", (int(user_id), key))

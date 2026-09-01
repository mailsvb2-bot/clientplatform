from __future__ import annotations

"""Shared operational tables used by the canonical ClientPlatform runtime.

These tables are intentionally product-neutral.  Historical product tables may
remain in upgraded databases for rollback/audit compatibility, but fresh
ClientPlatform bootstrap must not recreate demo, mood, practice, gift or legacy
subscription state as a side effect of creating jobs/events.
"""

import sqlite3

from services.schema_core import _add_col, _cols


def _ensure_users(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            joined_at TEXT,
            username TEXT,
            first_name TEXT
        )
        """.strip()
    )
    have = _cols(c, "users")
    for name, ddl in {
        "joined_at": "joined_at TEXT",
        "username": "username TEXT",
        "first_name": "first_name TEXT",
    }.items():
        if name not in have:
            _add_col(c, "users", ddl)


def _ensure_events(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event TEXT,
            ts TEXT,
            name TEXT,
            meta TEXT,
            created_at TEXT
        )
        """.strip()
    )
    have = _cols(c, "events")
    for name, ddl in {
        "event": "event TEXT",
        "ts": "ts TEXT",
        "name": "name TEXT",
        "meta": "meta TEXT",
        "created_at": "created_at TEXT",
    }.items():
        if name not in have:
            _add_col(c, "events", ddl)
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id)")


def _ensure_jobs(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            job_type TEXT NOT NULL,
            run_at_utc TEXT NOT NULL,
            payload TEXT,
            job_key TEXT,
            retries INTEGER DEFAULT 0,
            locked_at TEXT,
            lock_token TEXT,
            done_at TEXT,
            last_error TEXT
        )
        """.strip()
    )
    have = _cols(c, "jobs")
    for name, ddl in {
        "job_key": "job_key TEXT",
        "retries": "retries INTEGER DEFAULT 0",
        "locked_at": "locked_at TEXT",
        "lock_token": "lock_token TEXT",
        "done_at": "done_at TEXT",
        "last_error": "last_error TEXT",
    }.items():
        if name not in have:
            _add_col(c, "jobs", ddl)
    c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_at_utc)")
    c.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_job_key
        ON jobs(job_key)
        WHERE job_key IS NOT NULL
        """.strip()
    )


def _ensure_engine_state(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS engine_state(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at INTEGER
        )
        """.strip()
    )


def _ensure_privacy_audit(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS privacy_erasure_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            erased_at_utc TEXT NOT NULL,
            reason TEXT,
            retained_tables TEXT
        )
        """.strip()
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_privacy_erasure_user "
        "ON privacy_erasure_log(user_id, erased_at_utc)"
    )


def ensure(c: sqlite3.Connection) -> None:
    _ensure_users(c)
    _ensure_events(c)
    _ensure_jobs(c)
    _ensure_engine_state(c)
    _ensure_privacy_audit(c)

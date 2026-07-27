from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from services import body
from services.db.schema._parts import part_04


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE referrals(id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE mood_sessions(id INTEGER PRIMARY KEY)")
    part_04.ensure(conn)
    return conn


def test_body_feedback_is_one_record_per_user_and_session(monkeypatch) -> None:
    conn = _connection()

    @contextmanager
    def fake_db():
        with conn:
            yield conn

    monkeypatch.setattr(body, "db", fake_db)

    body.save_body_feedback(42, 7, "calm", "Шея")
    body.save_body_feedback(42, 7, "calm", "Плечи")

    rows = conn.execute(
        "SELECT user_id, session_id, kind, area FROM body_feedback"
    ).fetchall()
    assert len(rows) == 1
    assert dict(rows[0]) == {
        "user_id": 42,
        "session_id": 7,
        "kind": "calm",
        "area": "Плечи",
    }


def test_schema_collapses_legacy_duplicates_before_unique_index() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE referrals(id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE mood_sessions(id INTEGER PRIMARY KEY)")
    conn.execute(
        """
        CREATE TABLE body_feedback(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            area TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO body_feedback(session_id,user_id,kind,area,created_at_utc) VALUES(1,2,'x','old','1')"
    )
    conn.execute(
        "INSERT INTO body_feedback(session_id,user_id,kind,area,created_at_utc) VALUES(1,2,'x','new','2')"
    )

    part_04.ensure(conn)

    rows = conn.execute("SELECT area FROM body_feedback").fetchall()
    assert rows == [("new",)]
    with sqlite3.IntegrityError:
        pass
    try:
        conn.execute(
            "INSERT INTO body_feedback(session_id,user_id,kind,area,created_at_utc) VALUES(1,2,'x','duplicate','3')"
        )
    except sqlite3.IntegrityError:
        return
    raise AssertionError("body feedback unique index was not enforced")

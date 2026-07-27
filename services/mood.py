from __future__ import annotations
import logging
import sqlite3

log = logging.getLogger(__name__)


"""Self-assessment (до/после транса).

Требования (Установки):
- UX: без ввода текста, только быстрые кнопки.
- История НЕ обнуляется: данные копятся сколько угодно.
- БД миграции только вперёд: таблица создаётся в init_db().
- Никаких скрытых зависимостей: всё через services/db.py.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.time_utils import utcnow_iso
from services.db import db


def _write_changed_count(conn, cursor=None, *, table: str = "", id_column: str = "id", row_id=None) -> int:
    """Return number of changed rows in a DB-engine-neutral way.

    SQLite-only SELECT changes() breaks under Postgres wrappers. Prefer cursor.rowcount,
    then fall back to an existence check so callbacks do not crash after a successful UPDATE.
    """
    rowcount = getattr(cursor, "rowcount", None)
    try:
        if rowcount is not None and int(rowcount) >= 0:
            return int(rowcount)
    except (TypeError, ValueError):
        pass

    if table == "mood_sessions" and id_column == "id" and row_id is not None:
        try:
            row = conn.execute("SELECT 1 FROM mood_sessions WHERE id=? LIMIT 1", (row_id,)).fetchone()
            return 1 if row else 0
        except Exception:
            return 0

    return 0


@dataclass
class MoodSession:
    id: int
    user_id: int
    kind: str
    source: str
    day: str
    slot: str | None
    scheduled_at: str | None
    anchor_id: int | None
    pre_score: int | None
    post_score: int | None
    audio_sent: int


def create_session(
    user_id: int,
    *,
    kind: str,
    source: str,
    day: str,
    slot: str | None = None,
    scheduled_at: str | None = None,
    anchor_id: int | None = None,
) -> int:
    """Создаёт сессию оценки и возвращает её id."""

    with db() as conn:
        conn.execute(
            """
            INSERT INTO mood_sessions(user_id, kind, source, day, slot, scheduled_at, anchor_id, created_at_utc)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                int(user_id),
                (kind or "").strip() or "work",
                (source or "").strip() or "auto",
                str(day),
                (slot or "").strip() or None,
                (scheduled_at or "").strip() or None,
                int(anchor_id) if anchor_id is not None else None,
                utcnow_iso(),
            ),
        )
        return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def set_pre(session_id: int, score: int) -> bool:
    score = int(score)
    if score < -10 or score > 10:
        return False
    with db() as conn:
        conn.execute(
            "UPDATE mood_sessions SET pre_score=?, updated_at_utc=? WHERE id=?",
            (int(score), utcnow_iso(), int(session_id)),
        )
        n = _write_changed_count(conn, None, table="mood_sessions", id_column="id", row_id=session_id)
    return int(n) == 1


def set_post(session_id: int, score: int) -> bool:
    score = int(score)
    if score < -10 or score > 10:
        return False
    with db() as conn:
        conn.execute(
            "UPDATE mood_sessions SET post_score=?, updated_at_utc=? WHERE id=?",
            (int(score), utcnow_iso(), int(session_id)),
        )
        n = _write_changed_count(conn, None, table="mood_sessions", id_column="id", row_id=session_id)
    return int(n) == 1


def get_session(session_id: int) -> MoodSession | None:
    try:
        with db() as conn:
            r = conn.execute(
                "SELECT id,user_id,kind,source,day,slot,scheduled_at,anchor_id,pre_score,post_score,audio_sent "
                "FROM mood_sessions WHERE id=?",
                (int(session_id),),
            ).fetchone()
    except sqlite3.Error as e:
        log.exception("DB error in mood service: %s", e)
        return None
    if not r:
        return None
    return MoodSession(
        id=int(r["id"]),
        user_id=int(r["user_id"]),
        kind=str(r["kind"]),
        source=str(r["source"]),
        day=str(r["day"]),
        slot=str(r["slot"]) if r["slot"] is not None else None,
        scheduled_at=str(r["scheduled_at"]) if r["scheduled_at"] is not None else None,
        anchor_id=int(r["anchor_id"]) if r["anchor_id"] is not None else None,
        pre_score=int(r["pre_score"]) if r["pre_score"] is not None else None,
        post_score=int(r["post_score"]) if r["post_score"] is not None else None,
        audio_sent=int(r["audio_sent"]) if r["audio_sent"] is not None else 0,
    )


def get_user_session(session_id: int, user_id: int) -> MoodSession | None:
    """Return a mood session only when it belongs to the requesting user.

    Session ids appear in callback payloads and therefore are untrusted input.
    Centralizing the ownership check keeps every Telegram/messenger handler on
    the same fail-closed authorization boundary.
    """

    session = get_session(int(session_id))
    if session is None or int(session.user_id) != int(user_id):
        return None
    return session


def series(user_id: int, *, kind: str | None = None, limit: int = 120) -> list[dict[str, Any]]:
    """Return the newest bounded mood history in chronological order.

    Элементы: {"day": "YYYY-MM-DD", "pre": int|None, "post": int|None, "created": str}.
    The database selects newest rows first so ``LIMIT`` never freezes progress on
    the oldest records; the in-memory reversal preserves chart chronology.
    """
    try:
        limit = int(limit)
    except (ValueError, TypeError):
        log.warning("Invalid limit for mood series; using default 120")
        limit = 120
    limit = max(10, min(limit, 3650))

    if kind:
        q = (
            "SELECT day, pre_score, post_score, created_at_utc "
            "FROM mood_sessions WHERE user_id=? AND kind=? "
            "ORDER BY id DESC LIMIT ?"
        )
        params: list[Any] = [int(user_id), (kind or "").strip(), int(limit)]
    else:
        q = (
            "SELECT day, pre_score, post_score, created_at_utc "
            "FROM mood_sessions WHERE user_id=? "
            "ORDER BY id DESC LIMIT ?"
        )
        params = [int(user_id), int(limit)]

    try:
        with db() as conn:
            rows = conn.execute(q, params).fetchall()
    except sqlite3.Error as e:
        log.exception("DB error in mood service: %s", e)
        return []

    out: list[dict[str, Any]] = []
    for r in reversed(rows):
        out.append(
            {
                "day": str(r["day"]),
                "pre": int(r["pre_score"]) if r["pre_score"] is not None else None,
                "post": int(r["post_score"]) if r["post_score"] is not None else None,
                "created": str(r["created_at_utc"]),
            }
        )
    return out


def mark_audio_sent(session_id: int) -> None:
    """Помечает, что аудио уже отправлено по этой сессии."""
    try:
        with db() as conn:
            conn.execute(
                "UPDATE mood_sessions SET audio_sent=1, updated_at_utc=? WHERE id=?",
                (utcnow_iso(), int(session_id)),
            )
    except sqlite3.Error as e:
        log.exception("DB error in mood service: %s", e)


def last_delta(user_id: int, kind: str, *, limit: int = 30) -> dict[str, int | None]:
    """Возвращает простое сравнение: последняя сессия и средняя динамика.

    Output:
      {"last_pre": int|None, "last_post": int|None, "last_delta": int|None, "avg_delta": int|None}
    """
    rows = series(int(user_id), kind=(kind or None), limit=int(limit))
    if not rows:
        return {"last_pre": None, "last_post": None, "last_delta": None, "avg_delta": None}

    # average delta among rows where both scores present
    deltas = []
    for r in rows:
        pre, post = r.get("pre"), r.get("post")
        if pre is None or post is None:
            continue
        deltas.append(int(post) - int(pre))
    avg = int(round(sum(deltas) / len(deltas))) if deltas else None

    last = rows[-1]
    lp, lq = last.get("pre"), last.get("post")
    ld = (int(lq) - int(lp)) if (lp is not None and lq is not None) else None
    return {"last_pre": int(lp) if lp is not None else None, "last_post": int(lq) if lq is not None else None, "last_delta": ld, "avg_delta": avg}

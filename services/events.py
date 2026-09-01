from __future__ import annotations
import logging


import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from services.db import db


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def log_event(user_id: int, name: str, meta: Any | None = None, *, conn=None):
    """
    Логирование событий.
    ВАЖНО: если вызывается внутри транзакции, передаём conn=conn, чтобы не ловить 'database is locked'.
    """
    payload = json.dumps(meta or {}, ensure_ascii=False)

    if conn is not None:
        conn.execute(
            "INSERT INTO events(user_id, name, meta, created_at) VALUES(?,?,?,?)",
            (user_id, name, payload, _now_utc_iso()),
        )
        return

    with db() as c:
        c.execute(
            "INSERT INTO events(user_id, name, meta, created_at) VALUES(?,?,?,?)",
            (user_id, name, payload, _now_utc_iso()),
        )


def funnel_counts(names: list[str]) -> dict[str, int]:
    """Возвращает количество уникальных пользователей по каждому событию.

    Используется в админ-панели для быстрой воронки.
    """
    if not names:
        return {}

    res: dict[str, int] = {n: 0 for n in names}
    sql = "SELECT COUNT(DISTINCT user_id) AS cnt FROM events WHERE name=?"
    with db() as c:
        for name in names:
            row = c.execute(sql, (name,)).fetchone()
            try:
                res[name] = int((row[0] if row else 0) or 0)
            except (IndexError, TypeError, ValueError):
                logging.getLogger(__name__).exception("Bad row in events counts")
                res[name] = 0
    return res


def has_event_since(user_id: int, name: str, since_utc_iso: str) -> bool:
    """Есть ли у пользователя событие name, созданное не раньше since_utc_iso.

    since_utc_iso должен быть в формате ISO (как мы пишем в events.created_at).
    Функция нужна для идемпотентных проверок в автоворонке.
    """
    with db() as c:
        row = c.execute(
            "SELECT 1 FROM events WHERE user_id=? AND name=? AND created_at >= ? LIMIT 1",
            (int(user_id), str(name), str(since_utc_iso)),
        ).fetchone()
    return row is not None


# Cache PRAGMA table_info results per-process to avoid slow PRAGMA on every event write.
_EVENTS_COLS_CACHE: set[str] | None = None

def _events_cols(conn) -> set[str]:
    global _EVENTS_COLS_CACHE
    if _EVENTS_COLS_CACHE is None:
        try:
            _EVENTS_COLS_CACHE = {str(r[1]) for r in conn.execute("PRAGMA table_info(events)").fetchall()}
        except sqlite3.Error:
            _EVENTS_COLS_CACHE = {"user_id","name","meta","created_at"}
        except (IndexError, TypeError, ValueError):
            _EVENTS_COLS_CACHE = {"user_id","name","meta","created_at"}
    return _EVENTS_COLS_CACHE


def log_runtime_event(
    user_id: int,
    *,
    event_type: str,
    payload: Any | None = None,
    source: str = "telegram",
    correlation_id: str | None = None,
    decision_id: str | None = None,
    conn=None,
) -> None:
    """Write one best-effort runtime event without owning business decisions.

    Optional decision/correlation identifiers are accepted from the canonical
    caller when available; this shared logger never creates or authorizes them.
    """
    did = decision_id
    corr = correlation_id

    meta_obj = payload or {}
    payload_json = json.dumps(meta_obj, ensure_ascii=False)
    ts = _now_utc_iso()

    # Legacy columns (always present in our project DBs)
    name = str(event_type)

    def _write(c):
        cols = _events_cols(c)
        base_values = (int(user_id), name, payload_json, ts)

        # Full v2 schema.
        if {
            "event_type",
            "source",
            "payload",
            "timestamp_utc",
            "decision_id",
            "correlation_id",
        }.issubset(cols):
            c.execute(
                """
                INSERT INTO events(
                    user_id, name, meta, created_at,
                    event_type, source, payload, timestamp_utc, decision_id, correlation_id
                )
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (*base_values, name, str(source), payload_json, ts, did, corr),
            )
            return

        # Common v2-lite schema without decision/correlation columns.
        if {"event_type", "source", "payload", "timestamp_utc"}.issubset(cols):
            c.execute(
                """
                INSERT INTO events(
                    user_id, name, meta, created_at,
                    event_type, source, payload, timestamp_utc
                )
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (*base_values, name, str(source), payload_json, ts),
            )
            return

        # Legacy schema.
        c.execute(
            "INSERT INTO events(user_id, name, meta, created_at) VALUES(?,?,?,?)",
            base_values,
        )

    try:
        if conn is not None:
            _write(conn)
            return
        with db() as c:
            _write(c)
    except sqlite3.Error:
        logging.getLogger(__name__).exception("log_runtime_event failed (best-effort)")
    except (TypeError, ValueError, KeyError):
        logging.getLogger(__name__).exception("log_runtime_event failed (best-effort)")

from __future__ import annotations

import sqlite3

from services.schema_core import _cols, _add_col


def ensure(c: sqlite3.Connection) -> None:
    """Ensure tables/columns/indexes exist.

    Sections: QUICK STATE RATINGS ("как я прямо сейчас"), REFERRALS: расширение под PRO (бонус только за оплату), BONUS GRANTS (бухучёт бонусов "в днях"), MOOD SESSIONS: отметка отправки аудио (чтобы слать аудио сразу после pre-клика), BODY FEEDBACK (где в теле напряжение) привязано к сессии, Gift bonuses idempotency log (bonus for gifting paid gifts)
    """
    # QUICK STATE RATINGS ("как я прямо сейчас")
    # Отдельно от mood_sessions: сохраняем факт клика по цифре, даже если пользователь
    # не построил график сразу.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS state_ratings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            rating INTEGER NOT NULL,
            created_at_utc TEXT NOT NULL
        )
        """
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_state_ratings_user_time ON state_ratings(user_id, created_at_utc)")

    # REFERRALS: расширение под PRO (бонус только за оплату)
    have = _cols(c, "referrals")
    for k, ddl in {
        "paid_at": "paid_at TEXT",
        "bonus_applied": "bonus_applied INTEGER DEFAULT 0",
        "bonus_applied_at": "bonus_applied_at TEXT",
    }.items():
        if k not in have:
            _add_col(c, "referrals", ddl)



    # BONUS GRANTS (бухучёт бонусов "в днях")
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS bonus_grants(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            days INTEGER NOT NULL,
            source TEXT NOT NULL,          -- referral
            related_user_id INTEGER,       -- referred_id
            granted_at_utc TEXT NOT NULL
        )
        """
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_bonus_user_ts ON bonus_grants(user_id, granted_at_utc)")

    # MOOD SESSIONS: отметка отправки аудио (чтобы слать аудио сразу после pre-клика)
    have = _cols(c, "mood_sessions")
    if "audio_sent" not in have:
        _add_col(c, "mood_sessions", "audio_sent INTEGER DEFAULT 0")

    # BODY FEEDBACK (где в теле напряжение) привязано к сессии
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS body_feedback(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            area TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        )
        """
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_body_user_ts ON body_feedback(user_id, created_at_utc)")
    # Older builds allowed repeated callback writes. Keep the newest answer before
    # installing the database-level idempotency contract.
    c.execute(
        """
        DELETE FROM body_feedback
        WHERE id NOT IN (
            SELECT MAX(id) FROM body_feedback GROUP BY session_id, user_id
        )
        """
    )
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_body_feedback_session_user "
        "ON body_feedback(session_id, user_id)"
    )

    # Gift bonuses idempotency log (bonus for gifting paid gifts)
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS gift_bonus_log(
            code TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            bonus_days INTEGER NOT NULL,
            created_at_utc TEXT NOT NULL
        )
        """
    )

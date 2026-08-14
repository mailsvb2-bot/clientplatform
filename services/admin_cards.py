from __future__ import annotations

from services.db import db


def user_card(user_id: int) -> dict:
    """Карточка пользователя для админки (минимум, но полезно).

    Это legacy-карточка служебного Telegram-пользователя. Клиентская статистика
    ClientPlatform живёт в каноническом tenant-scoped customers/customer_identities
    и не должна считаться через таблицу users.
    """
    user_id = int(user_id)
    with db() as conn:
        u = conn.execute(
            "SELECT user_id, joined_at, username, first_name, work_time, home_time FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()

        sub = conn.execute(
            "SELECT scope, plan_type, total_morning, total_evening, used_morning, used_evening, status, started_at, paid_at FROM subscriptions WHERE user_id=?",
            (user_id,),
        ).fetchone()

        demo = conn.execute(
            "SELECT kind, sent_at_utc, ack_at_utc FROM demo_events WHERE user_id=? ORDER BY sent_at_utc DESC",
            (user_id,),
        ).fetchall()

        w = conn.execute(
            "SELECT city, lat, lon, updated_at FROM weather_prefs WHERE user_id=?",
            (user_id,),
        ).fetchone()

        ref = conn.execute(
            "SELECT referrer_id, reward_given, reward_days FROM referrals WHERE referred_id=?",
            (user_id,),
        ).fetchone()

        invited = conn.execute(
            "SELECT COUNT(1) AS n FROM referrals WHERE referrer_id=?",
            (user_id,),
        ).fetchone()

        beh = conn.execute(
            "SELECT ema_delta_ms, ema_absdev_ms, profile, updated_at FROM user_behavior WHERE user_id=?",
            (user_id,),
        ).fetchone()

        micro = conn.execute(
            "SELECT q_key, answer, ts FROM micro_answers WHERE user_id=? ORDER BY ts DESC LIMIT 10",
            (user_id,),
        ).fetchall()

    return {
        "user": dict(u) if u else None,
        "sub": dict(sub) if sub else None,
        "demo": [dict(r) for r in (demo or [])],
        "weather": dict(w) if w else None,
        "ref": dict(ref) if ref else None,
        "invited_count": int(invited["n"] or 0) if invited else 0,
        "behavior": dict(beh) if beh else None,
        "micro": [dict(r) for r in (micro or [])],
    }

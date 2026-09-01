from __future__ import annotations

from core.time_utils import utc_now
from services.db import db, tx
from services.events import log_event


class Store:
    """Minimal shared identity store used by messenger account registration."""

    def ensure_user(
        self,
        user_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ) -> None:
        uid = int(user_id)
        with db() as conn:
            with tx(conn):
                row = conn.execute(
                    "SELECT user_id FROM users WHERE user_id=?",
                    (uid,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO users(user_id, joined_at, username, first_name) VALUES(?,?,?,?)",
                        (uid, utc_now().replace(microsecond=0).isoformat(), username, first_name),
                    )
                    log_event(
                        uid,
                        "user_joined",
                        {"username": username, "first_name": first_name},
                        conn=conn,
                    )
                    return
                conn.execute(
                    "UPDATE users SET username=COALESCE(?, username), "
                    "first_name=COALESCE(?, first_name) WHERE user_id=?",
                    (username, first_name, uid),
                )


store = Store()

__all__ = ["Store", "store"]

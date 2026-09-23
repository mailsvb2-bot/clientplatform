from __future__ import annotations

import sqlite3

from services.migrations import messenger_delivery_permanent_rejection_v8 as migration


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE messenger_delivery_outbox(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            status TEXT NOT NULL,
            locked_at TEXT,
            lock_token TEXT,
            last_error TEXT NOT NULL DEFAULT ''
        )
        """.strip()
    )
    return conn


def test_migration_reclassifies_only_known_max_http_403_dead_letters() -> None:
    conn = _conn()
    try:
        rows = [
            ("max", "dead", "locked", "token", "MessengerTransportError:max.http.403"),
            ("max", "dead", "locked", "token", "MaxProviderRejectedError:max.send_text.http_403"),
            ("max", "dead", "locked", "token", "MessengerTransportError:max.http.500"),
            ("vk", "dead", "locked", "token", "MessengerTransportError:max.http.403"),
            ("max", "sent", None, None, "MessengerTransportError:max.http.403"),
        ]
        conn.executemany(
            "INSERT INTO messenger_delivery_outbox(platform,status,locked_at,lock_token,last_error) "
            "VALUES(?,?,?,?,?)",
            rows,
        )

        migration.apply(conn)

        result = conn.execute(
            "SELECT platform,status,locked_at,lock_token,last_error "
            "FROM messenger_delivery_outbox ORDER BY id"
        ).fetchall()

        assert [row["status"] for row in result] == [
            "rejected",
            "rejected",
            "dead",
            "dead",
            "sent",
        ]
        assert result[0]["locked_at"] is None
        assert result[0]["lock_token"] is None
        assert result[1]["locked_at"] is None
        assert result[1]["lock_token"] is None
    finally:
        conn.close()


def test_migration_is_idempotent() -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO messenger_delivery_outbox(platform,status,last_error) "
            "VALUES('max','dead','MessengerTransportError:max.http.403')"
        )
        migration.apply(conn)
        migration.apply(conn)

        row = conn.execute(
            "SELECT status FROM messenger_delivery_outbox"
        ).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM schema_migrations WHERE name=?",
            (migration.NAME,),
        ).fetchone()

        assert row["status"] == "rejected"
        assert int(count["count"]) == 1
    finally:
        conn.close()

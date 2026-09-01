from __future__ import annotations

from services.db import db
from services.privacy_controls import erase_user_behavioral_data, export_user_data_snapshot


def test_privacy_erase_anonymizes_global_identity_and_erases_behavior() -> None:
    uid = 987654321
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users(user_id, joined_at, username, first_name) VALUES(?,?,?,?)",
            (uid, "2026-01-01", "private_user", "Private"),
        )
        conn.execute(
            "INSERT INTO events(user_id, event, ts, meta) VALUES(?,?,?,?)",
            (uid, "clientplatform_event", "2026-01-01T00:00:00+00:00", "{}"),
        )
        conn.execute(
            """
            INSERT INTO accounts(account_id, primary_user_id, status, created_at, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET primary_user_id=excluded.primary_user_id, status=excluded.status
            """.strip(),
            (uid, uid, "active", "2026-01-01", "2026-01-01"),
        )
        conn.execute(
            """
            INSERT INTO account_channel_identities(
                account_id, platform, external_user_id, username, display_name,
                linked_at, last_seen_at, verified_at, link_source
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id, platform) DO UPDATE SET
                external_user_id=excluded.external_user_id,
                username=excluded.username,
                display_name=excluded.display_name
            """.strip(),
            (uid, "telegram", str(uid), "private_user", "Private User", "2026-01-01", "2026-01-01", None, "test"),
        )

    snapshot = export_user_data_snapshot(uid)
    assert snapshot["tables"]["users"][0]["username"] == "private_user"
    assert len(snapshot["tables"]["events"]) == 1
    assert snapshot["tables"]["account_channel_identities"][0]["display_name"] == "Private User"

    result = erase_user_behavioral_data(uid, reason="test")
    assert result.user_id == uid
    assert result.anonymized_profile is True
    assert result.deleted_tables["events"] == 1
    assert "accounts" in result.retained_tables
    assert "account_channel_identities" in result.retained_tables

    with db() as conn:
        user = conn.execute(
            "SELECT username, first_name FROM users WHERE user_id=?", (uid,)
        ).fetchone()
        assert user["username"] is None
        assert user["first_name"] is None
        identity = conn.execute(
            "SELECT external_user_id, username, display_name FROM account_channel_identities WHERE account_id=?",
            (uid,),
        ).fetchone()
        assert identity["external_user_id"] == str(uid)
        assert identity["username"] is None
        assert identity["display_name"] is None
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE user_id=?", (uid,)
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM privacy_erasure_log WHERE user_id=?", (uid,)
        ).fetchone()["c"] == 1

from __future__ import annotations

import sqlite3

import pytest

from services.db import db
from services.privacy_controls import erase_user_behavioral_data, export_user_data_snapshot
from services.privacy_manifest import MANIFEST_VERSION, POLICIES, validate_privacy_manifest


def test_current_schema_has_explicit_policy_for_every_user_owned_table() -> None:
    with db() as conn:
        report = validate_privacy_manifest(conn, strict=False)
    assert report.ok, {
        "unknown": report.unknown_tables,
        "invalid": report.invalid_policies,
        "missing_required": report.missing_required_tables,
    }
    for table in (
        "users",
        "accounts",
        "account_channel_identities",
        "messenger_delivery_outbox",
        "business_members",
    ):
        assert table in report.discovered_user_tables

def test_new_user_owned_table_fails_closed_until_policy_is_added() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE privacy_manifest_unknown_fixture(id INTEGER PRIMARY KEY, user_id INTEGER, payload TEXT)"
    )
    report = validate_privacy_manifest(conn, strict=False)
    assert "privacy_manifest_unknown_fixture" in report.unknown_tables
    with pytest.raises(RuntimeError, match="privacy_manifest_invalid"):
        validate_privacy_manifest(conn, strict=True)
    conn.close()


def test_retired_historical_tables_keep_explicit_non_required_privacy_policy() -> None:
    assert POLICIES["account_audio_progress"].disposition == "erase"
    assert POLICIES["payments"].disposition == "retain"
    assert POLICIES["sales_leads"].disposition == "anonymize"
    assert POLICIES["account_audio_progress"].required is False
    assert POLICIES["payments"].required is False
    assert POLICIES["sales_leads"].required is False

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE account_audio_progress(account_id INTEGER, value TEXT)")
    conn.execute("CREATE TABLE payments(user_id INTEGER, amount INTEGER)")
    report = validate_privacy_manifest(conn, strict=False)
    assert "account_audio_progress" in report.discovered_user_tables
    assert "payments" in report.discovered_user_tables
    assert "account_audio_progress" not in report.unknown_tables
    assert "payments" not in report.unknown_tables
    conn.close()


def test_non_business_unknown_ownership_column_still_fails_closed() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE privacy_manifest_buyer_fixture(id INTEGER PRIMARY KEY, buyer_user_id INTEGER)"
    )
    report = validate_privacy_manifest(conn, strict=False)
    assert "privacy_manifest_buyer_fixture" in report.unknown_tables
    conn.close()


def test_business_scoped_user_table_is_delegated_to_tenant_manifest() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tenant_owned_fixture(id INTEGER PRIMARY KEY, business_id INTEGER, user_id INTEGER)"
    )
    report = validate_privacy_manifest(conn, strict=False)
    assert "tenant_owned_fixture" not in report.discovered_user_tables
    assert "tenant_owned_fixture" not in report.unknown_tables
    conn.close()


def test_export_and_erasure_record_manifest_version() -> None:
    user_id = 885001
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users(user_id,username,first_name) VALUES(?,?,?)",
            (user_id, "privacy_manifest_user", "Manifest"),
        )
        conn.execute(
            "INSERT INTO events(user_id,event,ts,meta) VALUES(?,?,?,?)",
            (user_id, "manifest_event", "2026-07-17T00:00:00+00:00", "{}"),
        )
        conn.commit()

    snapshot = export_user_data_snapshot(user_id)
    assert snapshot["privacy_manifest_version"] == MANIFEST_VERSION
    assert snapshot["tables"]["users"][0]["username"] == "privacy_manifest_user"

    result = erase_user_behavioral_data(user_id, reason="manifest_test")
    assert result.manifest_version == MANIFEST_VERSION
    assert result.deleted_tables["events"] == 1
    with db() as conn:
        user = conn.execute(
            "SELECT username,first_name FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        audit = conn.execute(
            "SELECT retained_tables FROM privacy_erasure_log WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    assert user["username"] is None
    assert user["first_name"] is None
    assert MANIFEST_VERSION in str(audit["retained_tables"])

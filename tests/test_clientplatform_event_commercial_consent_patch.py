from __future__ import annotations

import hashlib
import sqlite3

import pytest

from clientplatform.application.event_commercial_consent import (
    CURRENT_EVENT_MARKETING_CONSENT_VERSION,
    active_event_commercial_channels,
    build_event_commercial_consent_text,
    commercial_consent_text_sha256,
    grant_event_commercial_consent_in_transaction,
    normalize_marketing_channels,
    revoke_event_commercial_consent_by_registration_token_in_transaction,
)
from services.db.schema import clientplatform_event_commercial_consents


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE businesses(id TEXT PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL);
        CREATE TABLE business_profiles(business_id TEXT PRIMARY KEY,brand_display_name TEXT);
        CREATE TABLE clientplatform_events(
            id TEXT PRIMARY KEY,business_id TEXT NOT NULL,public_slug TEXT NOT NULL,
            status TEXT NOT NULL,UNIQUE(id,business_id)
        );
        CREATE TABLE clientplatform_event_registrations(
            id TEXT PRIMARY KEY,event_id TEXT NOT NULL,business_id TEXT NOT NULL,
            status TEXT NOT NULL,token TEXT NOT NULL UNIQUE,identity_hash TEXT NOT NULL,
            source TEXT,campaign_ref TEXT,UNIQUE(id,business_id),UNIQUE(id,business_id,event_id)
        );
        CREATE TABLE provider_dispatch_outbox(
            id TEXT PRIMARY KEY,business_id TEXT,source_kind TEXT,source_id TEXT,
            idempotency_key TEXT,status TEXT,updated_at TEXT,locked_at TEXT,
            lock_token TEXT,last_error TEXT
        );
        """
    )
    clientplatform_event_commercial_consents.ensure(conn)
    conn.execute("INSERT INTO businesses VALUES('b','Business','active')")
    conn.execute("INSERT INTO business_profiles VALUES('b','Brand Name')")
    conn.execute("INSERT INTO clientplatform_events VALUES('e','b',?,'published')", ("s" * 24,))
    conn.execute(
        "INSERT INTO clientplatform_event_registrations VALUES(?,?,?,?,?,?,?,?)",
        ("r", "e", "b", "registered", "t" * 32, "a" * 64, "yandex", "cmp-1"),
    )
    return conn


def test_commercial_consent_copy_is_separate_versioned_and_hashable() -> None:
    text = build_event_commercial_consent_text("Brand Name")
    assert "рекламные сообщения" in text
    assert "отозвать" in text
    assert commercial_consent_text_sha256("Brand Name") == hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()
    assert CURRENT_EVENT_MARKETING_CONSENT_VERSION.startswith("2026-09-")


def test_public_channel_normalization_is_fail_closed() -> None:
    assert normalize_marketing_channels(["email", "max", "email"], public_form=True) == (
        "email",
        "max",
    )
    with pytest.raises(ValueError):
        normalize_marketing_channels([], public_form=True)
    with pytest.raises(ValueError):
        normalize_marketing_channels(["sms"], public_form=True)
    with pytest.raises(ValueError):
        normalize_marketing_channels(["telegram"], public_form=True)



def test_grant_rejects_a_consent_copy_hash_that_no_longer_matches() -> None:
    conn = _conn()
    with pytest.raises(ValueError, match="consent text changed"):
        grant_event_commercial_consent_in_transaction(
            conn,
            business_id="b",
            event_id="e",
            registration_id="r",
            channels=("email",),
            expected_text_sha256="0" * 64,
            now="2026-09-11T10:00:00+00:00",
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM clientplatform_event_commercial_consent_events"
    ).fetchone()[0] == 0

def test_grant_is_auditable_and_revoke_stops_not_started_commercial_work() -> None:
    conn = _conn()
    evidence = grant_event_commercial_consent_in_transaction(
        conn,
        business_id="b",
        event_id="e",
        registration_id="r",
        channels=("email", "max"),
        now="2026-09-11T10:00:00+00:00",
    )
    assert evidence.advertiser_label == "Brand Name"
    assert set(
        active_event_commercial_channels(
            conn, business_id="b", event_id="e", registration_id="r"
        )
    ) == {"email", "max"}
    row = conn.execute(
        "SELECT action,consent_text_version,consent_text_sha256,channels_csv,registration_identity_hash "
        "FROM clientplatform_event_commercial_consent_events"
    ).fetchone()
    assert row["action"] == "grant"
    assert row["consent_text_version"] == CURRENT_EVENT_MARKETING_CONSENT_VERSION
    assert len(row["consent_text_sha256"]) == 64
    assert row["channels_csv"] == "email,max"
    assert row["registration_identity_hash"] == "a" * 64

    conn.execute(
        """
        INSERT INTO provider_dispatch_outbox(
            id,business_id,source_kind,source_id,idempotency_key,status,updated_at
        ) VALUES('d','b','event_message','r',
            'event:e:registration:r:message:post:v4:stage:1','pending','2026-09-11T10:00:00+00:00')
        """
    )
    revoked = revoke_event_commercial_consent_by_registration_token_in_transaction(
        conn,
        token="t" * 32,
        now="2026-09-11T10:05:00+00:00",
    )
    assert revoked == 2
    assert active_event_commercial_channels(
        conn, business_id="b", event_id="e", registration_id="r"
    ) == ()
    dispatch = conn.execute(
        "SELECT status,last_error FROM provider_dispatch_outbox WHERE id='d'"
    ).fetchone()
    assert tuple(dispatch) == ("cancelled", "event_commercial_consent_revoked")
    assert conn.execute(
        "SELECT COUNT(*) FROM clientplatform_event_commercial_consent_events WHERE action='revoke'"
    ).fetchone()[0] == 1

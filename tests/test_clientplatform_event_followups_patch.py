from __future__ import annotations

import sqlite3

from clientplatform.application.event_commercial_consent import (
    grant_event_commercial_consent_in_transaction,
)
from clientplatform.application.event_followups import (
    classify_event_followup_segment,
    commercial_event_followups_enabled,
    materialize_due_event_followups_in_transaction,
)
from services.db.schema import clientplatform_event_commercial_consents


def _conn(*, with_max: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE businesses(id TEXT PRIMARY KEY,name TEXT,status TEXT);
        CREATE TABLE business_profiles(business_id TEXT PRIMARY KEY,brand_display_name TEXT);
        CREATE TABLE clientplatform_events(
            id TEXT PRIMARY KEY,business_id TEXT,title TEXT,starts_at TEXT,ends_at TEXT,
            notification_connection_id TEXT,offer_url TEXT,status TEXT,public_slug TEXT,
            UNIQUE(id,business_id)
        );
        CREATE TABLE clientplatform_event_registrations(
            id TEXT PRIMARY KEY,event_id TEXT,business_id TEXT,customer_id TEXT,status TEXT,
            name TEXT,email TEXT,token TEXT,identity_hash TEXT,source TEXT,campaign_ref TEXT,
            first_join_click_at TEXT,attendance_confirmed_at TEXT,offer_clicked_at TEXT,
            UNIQUE(id,business_id),UNIQUE(id,business_id,event_id)
        );
        CREATE TABLE clientplatform_event_conversion_links(
            business_id TEXT,event_id TEXT,registration_id TEXT,payment_ref TEXT,
            PRIMARY KEY(business_id,event_id,registration_id,payment_ref)
        );
        CREATE TABLE connections(
            id TEXT PRIMARY KEY,business_id TEXT,platform TEXT,connection_type TEXT,
            status TEXT,created_at TEXT
        );
        CREATE TABLE customer_identities(
            id TEXT PRIMARY KEY,business_id TEXT,customer_id TEXT,platform TEXT,
            external_subject TEXT,status TEXT,last_contact_at TEXT,updated_at TEXT,created_at TEXT
        );
        CREATE TABLE provider_dispatch_outbox(
            id TEXT PRIMARY KEY,business_id TEXT,platform TEXT,source_kind TEXT,source_id TEXT,
            logical_delivery_id TEXT,partner_campaign_id TEXT,partner_candidate_id TEXT,
            sales_followup_id TEXT,connection_id TEXT,recipient_kind TEXT,customer_identity_id TEXT,
            external_subject TEXT,payload_kind TEXT,payload_ref TEXT,idempotency_key TEXT,status TEXT,
            attempts INTEGER,available_at TEXT,locked_at TEXT,lock_token TEXT,provider_message_id TEXT,
            last_error TEXT,created_at TEXT,updated_at TEXT,sent_at TEXT,dead_at TEXT,
            UNIQUE(business_id,idempotency_key)
        );
        """
    )
    clientplatform_event_commercial_consents.ensure(conn)
    conn.execute("INSERT INTO businesses VALUES('b','Business','active')")
    conn.execute("INSERT INTO business_profiles VALUES('b','Brand')")
    conn.execute(
        "INSERT INTO connections VALUES('emailc','b','email','email_smtp','active','2026-09-01T00:00:00+00:00')"
    )
    if with_max:
        conn.execute(
            "INSERT INTO connections VALUES('maxc','b','max','max_shared_bot','active','2026-09-01T00:00:00+00:00')"
        )
    conn.execute(
        "INSERT INTO clientplatform_events VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "e",
            "b",
            "Webinar",
            "2026-09-11T10:00:00+00:00",
            "2026-09-11T12:00:00+00:00",
            "emailc",
            "https://example.test/offer",
            "completed",
            "s" * 24,
        ),
    )
    conn.execute(
        "INSERT INTO clientplatform_event_registrations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "r",
            "e",
            "b",
            "cust",
            "registered",
            "Ivan",
            "ivan@example.test",
            "t" * 32,
            "a" * 64,
            "yandex",
            "campaign-1",
            "2026-09-11T10:00:00+00:00",
            "2026-09-11T10:05:00+00:00",
            "2026-09-11T11:00:00+00:00",
        ),
    )
    if with_max:
        conn.execute(
            "INSERT INTO customer_identities VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "ident",
                "b",
                "cust",
                "max",
                "123456",
                "active",
                "2026-09-11T11:00:00+00:00",
                "2026-09-11T11:00:00+00:00",
                "2026-09-01T00:00:00+00:00",
            ),
        )
    return conn


def test_event_followup_feature_is_fail_closed(monkeypatch) -> None:
    monkeypatch.delenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", raising=False)
    assert commercial_event_followups_enabled() is False
    monkeypatch.setenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", "true")
    assert commercial_event_followups_enabled() is True


def test_event_followup_segments_do_not_confuse_join_with_attendance() -> None:
    assert classify_event_followup_segment(
        first_join_click_at="x", attendance_confirmed_at=None, offer_clicked_at=None
    ) == "join_signal_unpaid"
    assert classify_event_followup_segment(
        first_join_click_at="x", attendance_confirmed_at="y", offer_clicked_at=None
    ) == "attended_unpaid"
    assert classify_event_followup_segment(
        first_join_click_at="x", attendance_confirmed_at="y", offer_clicked_at="z"
    ) == "offer_clicked_unpaid"


def test_followup_prefers_consented_max_and_includes_unsubscribe(monkeypatch) -> None:
    conn = _conn(with_max=True)
    grant_event_commercial_consent_in_transaction(
        conn,
        business_id="b",
        event_id="e",
        registration_id="r",
        channels=("email", "max"),
        now="2026-09-11T09:00:00+00:00",
    )
    monkeypatch.setenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", "true")
    monkeypatch.setenv("CLIENTPLATFORM_EVENT_FOLLOWUP_CHANNEL_PRIORITY", "max,vk,email")
    monkeypatch.setattr(
        "clientplatform.application.event_followups._public_base_url",
        lambda: "https://clientplatform.example",
    )
    result = materialize_due_event_followups_in_transaction(
        conn, now="2026-09-11T13:00:00+00:00"
    )
    assert result.queued == 1
    row = conn.execute(
        "SELECT platform,connection_id,recipient_kind,customer_identity_id,payload_ref,idempotency_key "
        "FROM provider_dispatch_outbox"
    ).fetchone()
    assert row["platform"] == "max"
    assert row["connection_id"] == "maxc"
    assert row["recipient_kind"] == "customer_identity"
    assert row["customer_identity_id"] == "ident"
    assert "/e/marketing/unsubscribe/" in row["payload_ref"]
    assert row["idempotency_key"].endswith(":message:post:v4:stage:1")


def test_followup_falls_back_to_consented_email_when_native_route_is_ambiguous(
    monkeypatch,
) -> None:
    conn = _conn(with_max=False)
    grant_event_commercial_consent_in_transaction(
        conn,
        business_id="b",
        event_id="e",
        registration_id="r",
        channels=("email", "max"),
        now="2026-09-11T09:00:00+00:00",
    )
    monkeypatch.setenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", "true")
    monkeypatch.setattr(
        "clientplatform.application.event_followups._public_base_url",
        lambda: "https://clientplatform.example",
    )
    result = materialize_due_event_followups_in_transaction(
        conn, now="2026-09-11T13:00:00+00:00"
    )
    assert result.queued == 1
    row = conn.execute(
        "SELECT platform,connection_id,recipient_kind FROM provider_dispatch_outbox"
    ).fetchone()
    assert tuple(row) == ("email", "emailc", "external_subject")


def test_paid_registration_is_terminal_and_never_queues_followup(monkeypatch) -> None:
    conn = _conn(with_max=True)
    grant_event_commercial_consent_in_transaction(
        conn,
        business_id="b",
        event_id="e",
        registration_id="r",
        channels=("email", "max"),
        now="2026-09-11T09:00:00+00:00",
    )
    conn.execute(
        "INSERT INTO clientplatform_event_conversion_links VALUES('b','e','r','verified-payment')"
    )
    monkeypatch.setenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", "true")
    result = materialize_due_event_followups_in_transaction(
        conn, now="2026-09-11T13:00:00+00:00"
    )
    assert result.queued == 0
    assert conn.execute("SELECT COUNT(*) FROM provider_dispatch_outbox").fetchone()[0] == 0


def test_enabling_new_engine_cancels_legacy_offer_after_message(monkeypatch) -> None:
    conn = _conn(with_max=False)
    conn.execute(
        """
        INSERT INTO provider_dispatch_outbox(
            id,business_id,platform,source_kind,source_id,connection_id,recipient_kind,
            external_subject,payload_kind,payload_ref,idempotency_key,status,attempts,
            available_at,created_at,updated_at
        ) VALUES(
            'legacy','b','email','event_message','r','emailc','external_subject',
            'ivan@example.test','mixed','{}',
            'event:e:registration:r:message:after:v2','pending',0,
            '2026-09-11T12:15:00+00:00','2026-09-11T00:00:00+00:00','2026-09-11T00:00:00+00:00'
        )
        """
    )
    monkeypatch.setenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", "true")
    result = materialize_due_event_followups_in_transaction(
        conn, now="2026-09-11T13:00:00+00:00"
    )
    assert result.legacy_after_cancelled == 1
    row = conn.execute(
        "SELECT status,last_error FROM provider_dispatch_outbox WHERE id='legacy'"
    ).fetchone()
    assert tuple(row) == (
        "cancelled",
        "replaced_by_consent_aware_event_followup",
    )

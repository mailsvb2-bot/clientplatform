from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from clientplatform.application.event_commercial_consent import (
    grant_event_commercial_consent_in_transaction,
)
from clientplatform.application.event_followups import (
    classify_event_followup_segment,
    commercial_event_followups_enabled,
    materialize_due_event_followups_in_transaction,
)
from clientplatform.domain.automation_policy import (
    AutomationMode,
    AutomationPolicySpec,
    AutomationSchedule,
)
from clientplatform.infrastructure.automation_policy_repository import AutomationPolicyRepository
from clientplatform.infrastructure.event_dispatch_safety import event_commercial_policy_authorized
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import clientplatform_event_commercial_consents, create_or_update_tables


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
        CREATE TABLE clientplatform_event_followup_scan_state(
            scope TEXT PRIMARY KEY,cursor_starts_at TEXT,cursor_event_id TEXT,
            cursor_registration_id TEXT,updated_at TEXT NOT NULL
        );
        CREATE TABLE clientplatform_event_followup_settings(
            business_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL,
            segment_no_show INTEGER NOT NULL DEFAULT 1,segment_join_signal INTEGER NOT NULL DEFAULT 1,
            segment_attended INTEGER NOT NULL DEFAULT 1,segment_offer_clicked INTEGER NOT NULL DEFAULT 1,
            channel_email INTEGER NOT NULL DEFAULT 1,channel_max INTEGER NOT NULL DEFAULT 1,channel_vk INTEGER NOT NULL DEFAULT 1,
            settings_epoch INTEGER NOT NULL,updated_by_member_id TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
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
        "INSERT INTO clientplatform_event_followup_settings(business_id,enabled,settings_epoch,updated_by_member_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("b", 1, 1, "owner-member", "2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
    )
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


def test_event_followup_platform_gate_defaults_available_but_explicit_false_kills(monkeypatch) -> None:
    monkeypatch.delenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", raising=False)
    assert commercial_event_followups_enabled() is True
    monkeypatch.setenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", "false")
    assert commercial_event_followups_enabled() is False
    monkeypatch.setenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", "true")
    assert commercial_event_followups_enabled() is True


def test_event_followup_business_setting_defaults_off_even_when_platform_available(monkeypatch) -> None:
    conn = _conn(with_max=False)
    conn.execute("DELETE FROM clientplatform_event_followup_settings")
    monkeypatch.delenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", raising=False)
    result = materialize_due_event_followups_in_transaction(
        conn, now="2026-09-11T13:00:00+00:00"
    )
    assert result.queued == 0
    assert result.business_disabled == 1


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
    monkeypatch.setattr(
        "clientplatform.application.event_followups._automation_followup_authorized",
        lambda *args, **kwargs: True,
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
    monkeypatch.setattr(
        "clientplatform.application.event_followups._automation_followup_authorized",
        lambda *args, **kwargs: True,
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


def test_disabled_new_engine_still_cancels_legacy_commercial_after_message(monkeypatch) -> None:
    conn = _conn(with_max=False)
    conn.execute(
        """
        INSERT INTO provider_dispatch_outbox(
            id,business_id,platform,source_kind,source_id,connection_id,recipient_kind,
            external_subject,payload_kind,payload_ref,idempotency_key,status,attempts,
            available_at,created_at,updated_at
        ) VALUES(
            'legacy-off','b','email','event_message','r','emailc','external_subject',
            'ivan@example.test','mixed','{}',
            'event:e:registration:r:message:after:v2','pending',0,
            '2026-09-11T12:15:00+00:00','2026-09-11T00:00:00+00:00','2026-09-11T00:00:00+00:00'
        )
        """
    )
    monkeypatch.delenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", raising=False)
    result = materialize_due_event_followups_in_transaction(
        conn, now="2026-09-11T13:00:00+00:00"
    )
    assert result.legacy_after_cancelled == 1
    row = conn.execute(
        "SELECT status FROM provider_dispatch_outbox WHERE id='legacy-off'"
    ).fetchone()
    assert row[0] == "cancelled"


def test_durable_scan_cursor_bounds_work_and_reaches_later_eligible_registration(monkeypatch) -> None:
    conn = _conn(with_max=False)
    for index in range(120):
        registration_id = f"r{index:03d}"
        conn.execute(
            "INSERT INTO clientplatform_event_registrations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                registration_id, "e", "b", None, "registered", f"User {index}",
                f"user{index}@example.test", f"token{index:027d}", f"{index + 1:064x}",
                "yandex", "campaign-1", None, "2026-09-11T10:05:00+00:00", None,
            ),
        )
    grant_event_commercial_consent_in_transaction(
        conn, business_id="b", event_id="e", registration_id="r119",
        channels=("email",), now="2026-09-11T09:00:00+00:00",
    )
    monkeypatch.setenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", "true")
    monkeypatch.setattr(
        "clientplatform.application.event_followups._public_base_url",
        lambda: "https://clientplatform.example",
    )
    monkeypatch.setattr(
        "clientplatform.application.event_followups._automation_followup_authorized",
        lambda *args, **kwargs: True,
    )

    results = []
    for _ in range(8):
        result = materialize_due_event_followups_in_transaction(
            conn, limit=25, now="2026-09-11T13:00:00+00:00"
        )
        results.append(result)
        assert result.scanned <= 25
        if result.queued:
            break

    assert sum(result.queued for result in results) == 1
    assert len(results) > 1
    row = conn.execute(
        "SELECT source_id FROM provider_dispatch_outbox WHERE idempotency_key LIKE '%:post:v4:stage:1'"
    ).fetchone()
    assert row[0] == "r119"
    cursor = conn.execute(
        "SELECT cursor_registration_id FROM clientplatform_event_followup_scan_state "
        "WHERE scope='commercial-event-followups:v1'"
    ).fetchone()
    assert cursor is not None
    assert cursor[0] == "r119"


def test_followup_stages_are_serialized_by_previous_delivery_time(monkeypatch) -> None:
    conn = _conn(with_max=False)
    grant_event_commercial_consent_in_transaction(
        conn, business_id="b", event_id="e", registration_id="r",
        channels=("email",), now="2026-09-11T09:00:00+00:00",
    )
    monkeypatch.setenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", "true")
    monkeypatch.setattr(
        "clientplatform.application.event_followups._public_base_url",
        lambda: "https://clientplatform.example",
    )
    monkeypatch.setattr(
        "clientplatform.application.event_followups._automation_followup_authorized",
        lambda *args, **kwargs: True,
    )

    first = materialize_due_event_followups_in_transaction(
        conn, now="2026-09-11T13:00:00+00:00"
    )
    assert first.queued == 1
    conn.execute(
        "UPDATE provider_dispatch_outbox SET status='sent',sent_at=?,updated_at=? "
        "WHERE idempotency_key LIKE '%:post:v4:stage:1'",
        ("2026-09-11T13:00:00+00:00", "2026-09-11T13:00:00+00:00"),
    )

    second = materialize_due_event_followups_in_transaction(
        conn, now="2026-09-13T12:00:00+00:00"
    )
    assert second.queued == 1
    stages = conn.execute(
        "SELECT idempotency_key,status FROM provider_dispatch_outbox ORDER BY idempotency_key"
    ).fetchall()
    assert [row[0].rsplit(':', 1)[-1] for row in stages] == ["1", "2"]
    conn.execute(
        "UPDATE provider_dispatch_outbox SET status='sent',sent_at=?,updated_at=? "
        "WHERE idempotency_key LIKE '%:post:v4:stage:2'",
        ("2026-09-13T12:00:00+00:00", "2026-09-13T12:00:00+00:00"),
    )

    immediate = materialize_due_event_followups_in_transaction(
        conn, now="2026-09-13T12:01:00+00:00"
    )
    assert immediate.queued == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM provider_dispatch_outbox "
        "WHERE idempotency_key LIKE '%:post:v4:stage:3'"
    ).fetchone()[0] == 0

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


def test_event_followup_requires_owner_approved_policy_and_honors_quiet_hours(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    create_or_update_tables(conn)
    tenancy = TenancyRepository(conn)
    access = tenancy.create_business(
        owner_user_id=901,
        name="Policy Event",
        now="2026-09-12T09:00:00+00:00",
    )
    owner = tenancy.resolve_context(user_id=901, business_id=access.business.id)
    registration_id = str(uuid4())
    payload_ref = "commercial-payload"

    assert not event_commercial_policy_authorized(
        conn,
        business_id=owner.business_id,
        registration_id=registration_id,
        platform="email",
        payload_ref=payload_ref,
        scheduled_at="2026-09-12T12:00:00+00:00",
        now="2026-09-12T12:00:00+00:00",
    )

    spec = AutomationPolicySpec(
        mode=AutomationMode.AUTOPILOT,
        allowed_actions=("events.commercial_followup",),
        forbidden_actions=(),
        allowed_channels=("email",),
        allowed_audiences=("prospect_opted_in",),
        schedule=AutomationSchedule(
            timezone_name="UTC",
            quiet_start="22:00",
            quiet_end="08:00",
        ),
        expires_at="2026-10-12T00:00:00+00:00",
        allowed_content_topics=("service_offer",),
        stop_conditions=("owner_stop", "business_suspended"),
    )
    repository = AutomationPolicyRepository(conn)
    draft = repository.create_draft(
        actor=owner, spec=spec, expected_latest_version=0,
        now="2026-09-12T09:00:00+00:00",
    )
    repository.approve(
        actor=owner, policy_id=draft.id, expected_policy_hash=draft.policy_hash,
        now="2026-09-12T09:01:00+00:00",
    )

    assert event_commercial_policy_authorized(
        conn,
        business_id=owner.business_id,
        registration_id=registration_id,
        platform="email",
        payload_ref=payload_ref,
        scheduled_at="2026-09-12T12:00:00+00:00",
        now="2026-09-12T12:00:00+00:00",
    )
    assert not event_commercial_policy_authorized(
        conn,
        business_id=owner.business_id,
        registration_id=registration_id,
        platform="email",
        payload_ref=payload_ref,
        scheduled_at="2026-09-12T23:00:00+00:00",
        now="2026-09-12T23:00:00+00:00",
    )
    assert not event_commercial_policy_authorized(
        conn,
        business_id=owner.business_id,
        registration_id=registration_id,
        platform="email",
        payload_ref=payload_ref,
        scheduled_at="2026-09-12T12:00:00+00:00",
        now="2026-09-12T23:00:00+00:00",
    )
    conn.close()


def test_disabled_participant_group_never_queues_message(monkeypatch) -> None:
    conn = _conn(with_max=True)
    conn.execute(
        "UPDATE clientplatform_event_followup_settings SET segment_offer_clicked=0 WHERE business_id='b'"
    )
    grant_event_commercial_consent_in_transaction(
        conn, business_id="b", event_id="e", registration_id="r",
        channels=("email", "max"), now="2026-09-11T09:00:00+00:00",
    )
    monkeypatch.setenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", "true")
    result = materialize_due_event_followups_in_transaction(
        conn, now="2026-09-11T13:00:00+00:00"
    )
    assert result.queued == 0
    assert result.strategy_blocked == 1
    assert conn.execute("SELECT COUNT(*) FROM provider_dispatch_outbox").fetchone()[0] == 0


def test_disabled_max_channel_falls_back_to_email(monkeypatch) -> None:
    conn = _conn(with_max=True)
    conn.execute(
        "UPDATE clientplatform_event_followup_settings SET channel_max=0 WHERE business_id='b'"
    )
    grant_event_commercial_consent_in_transaction(
        conn, business_id="b", event_id="e", registration_id="r",
        channels=("email", "max"), now="2026-09-11T09:00:00+00:00",
    )
    monkeypatch.setenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", "true")
    monkeypatch.setattr(
        "clientplatform.application.event_followups._public_base_url",
        lambda: "https://clientplatform.example",
    )
    monkeypatch.setattr(
        "clientplatform.application.event_followups._automation_followup_authorized",
        lambda *args, **kwargs: True,
    )
    result = materialize_due_event_followups_in_transaction(
        conn, now="2026-09-11T13:00:00+00:00"
    )
    assert result.queued == 1
    row = conn.execute("SELECT platform FROM provider_dispatch_outbox").fetchone()
    assert row[0] == "email"

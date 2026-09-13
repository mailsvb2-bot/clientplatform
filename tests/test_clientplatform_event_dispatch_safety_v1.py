from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import sqlite3
from unittest.mock import patch

from clientplatform.domain.connections import ConnectionPlatform, DispatchStatus
from clientplatform.domain.programs import ContentKind
from clientplatform.infrastructure.event_dispatch_safety import (
    event_commercial_claim_can_cross_provider_boundary,
    mark_event_commercial_non_replay_boundary,
    quarantine_stale_event_commercial_boundaries,
)
from clientplatform.infrastructure.event_safe_dispatch_outbox import (
    DispatchOutboxRepository as EventSafeDispatchOutboxRepository,
)
from clientplatform.infrastructure.unified_dispatch_outbox import (
    ClaimedProviderDispatch,
    ProviderDispatch,
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE provider_dispatch_outbox(
          id TEXT,business_id TEXT,platform TEXT,source_kind TEXT,source_id TEXT,
          connection_id TEXT,customer_identity_id TEXT,external_subject TEXT,
          idempotency_key TEXT,status TEXT,lock_token TEXT,locked_at TEXT,
          last_error TEXT,updated_at TEXT,dead_at TEXT
        );
        CREATE TABLE clientplatform_event_registrations(
          id TEXT,business_id TEXT,event_id TEXT,status TEXT,email TEXT,customer_id TEXT,
          first_join_click_at TEXT,attendance_confirmed_at TEXT,offer_clicked_at TEXT
        );
        CREATE TABLE clientplatform_events(id TEXT,business_id TEXT,status TEXT);
        CREATE TABLE businesses(id TEXT,status TEXT);
        CREATE TABLE connections(id TEXT,business_id TEXT,platform TEXT,status TEXT,connection_type TEXT);
        CREATE TABLE customer_identities(
          id TEXT,business_id TEXT,platform TEXT,status TEXT,customer_id TEXT,external_subject TEXT
        );
        CREATE TABLE clientplatform_event_conversion_links(
          business_id TEXT,event_id TEXT,registration_id TEXT
        );
        CREATE TABLE clientplatform_event_commercial_channel_state(
          business_id TEXT,event_id TEXT,registration_id TEXT,platform TEXT,status TEXT
        );
        CREATE TABLE clientplatform_event_followup_settings(
          business_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL,
          segment_no_show INTEGER NOT NULL DEFAULT 1,segment_join_signal INTEGER NOT NULL DEFAULT 1,
          segment_attended INTEGER NOT NULL DEFAULT 1,segment_offer_clicked INTEGER NOT NULL DEFAULT 1,
          channel_email INTEGER NOT NULL DEFAULT 1,channel_max INTEGER NOT NULL DEFAULT 1,channel_vk INTEGER NOT NULL DEFAULT 1,
          settings_epoch INTEGER NOT NULL,updated_by_member_id TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO businesses VALUES('b','active')")
    conn.execute(
        "INSERT INTO clientplatform_event_followup_settings(business_id,enabled,settings_epoch,updated_by_member_id,created_at,updated_at) VALUES('b',1,1,'owner-member','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')"
    )
    conn.execute("INSERT INTO clientplatform_events VALUES('e','b','completed')")
    conn.execute("INSERT INTO clientplatform_event_registrations VALUES('r','b','e','registered','a@example.test',NULL,NULL,NULL,NULL)")
    conn.execute("INSERT INTO connections VALUES('c','b','email','active','email_smtp')")
    conn.execute("INSERT INTO clientplatform_event_commercial_channel_state VALUES('b','e','r','email','active')")
    conn.execute(
        "INSERT INTO provider_dispatch_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ('d','b','email','event_message','r','c',None,'a@example.test',
         'event:e:registration:r:message:post:v4:stage:1','sending','lock',
         '2026-09-12T10:00:00+00:00',None,'2026-09-12T10:00:00+00:00',None),
    )
    return conn


def _item() -> ClaimedProviderDispatch:
    return ClaimedProviderDispatch(
        dispatch=ProviderDispatch(
            id='d',business_id='b',platform=ConnectionPlatform.EMAIL,
            source_kind='event_message',source_id='r',connection_id='c',
            external_subject='a@example.test',payload_kind=ContentKind.TEXT,
            payload_ref='hello',idempotency_key='event:e:registration:r:message:post:v4:stage:1',
            status=DispatchStatus.SENDING,attempts=0,
            available_at='2026-09-12T10:00:00+00:00',
            created_at='2026-09-12T10:00:00+00:00',updated_at='2026-09-12T10:00:00+00:00',
            locked_at='2026-09-12T10:00:00+00:00',lock_token='lock',
        ),
        external_subject='a@example.test',credential_reference='smtp',
    )


def test_commercial_event_authority_rechecks_consent_and_paid_state() -> None:
    conn = _db(); item = _item()
    with patch(
        "clientplatform.infrastructure.event_dispatch_safety.event_commercial_policy_authorized",
        return_value=True,
    ):
        assert event_commercial_claim_can_cross_provider_boundary(conn, item)
    conn.execute("INSERT INTO clientplatform_event_conversion_links VALUES('b','e','r')")
    assert not event_commercial_claim_can_cross_provider_boundary(conn, item, now='2026-09-12T10:01:00+00:00')
    row = conn.execute("SELECT status,last_error FROM provider_dispatch_outbox WHERE id='d'").fetchone()
    assert tuple(row) == ('cancelled','event_message_authority_revoked_or_paid')


def test_business_switch_off_wins_before_non_replay_marker() -> None:
    conn = _db(); item = _item()
    conn.execute("UPDATE clientplatform_event_followup_settings SET enabled=0,settings_epoch=2")
    assert not mark_event_commercial_non_replay_boundary(
        conn, item, now='2026-09-12T10:00:01+00:00'
    )
    row = conn.execute(
        "SELECT status,last_error FROM provider_dispatch_outbox WHERE id='d'"
    ).fetchone()
    assert tuple(row) == ('cancelled','event_commercial_business_disabled')


def test_commercial_event_boundary_becomes_non_replayable() -> None:
    conn = _db(); item = _item()
    assert mark_event_commercial_non_replay_boundary(conn, item, now='2026-09-12T10:00:01+00:00')
    marker = conn.execute("SELECT last_error FROM provider_dispatch_outbox WHERE id='d'").fetchone()[0]
    assert marker == 'event_commercial_provider_call_started_non_idempotent'
    quarantined = quarantine_stale_event_commercial_boundaries(
        conn,
        lock_ttl_seconds=60,
        now=datetime(2026, 9, 12, 10, 2, tzinfo=timezone.utc),
    )
    assert quarantined == 1
    row = conn.execute("SELECT status,last_error FROM provider_dispatch_outbox WHERE id='d'").fetchone()
    assert tuple(row) == ('dead','event_commercial_delivery_outcome_ambiguous_manual_reconciliation_required')


def test_revoked_commercial_consent_blocks_provider_boundary() -> None:
    conn = _db(); item = _item()
    conn.execute("UPDATE clientplatform_event_commercial_channel_state SET status='revoked'")
    assert not event_commercial_claim_can_cross_provider_boundary(conn, item)


def test_quarantine_does_not_write_shared_outbox_without_stale_event_work() -> None:
    conn = _db()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    quarantined = quarantine_stale_event_commercial_boundaries(
        conn,
        lock_ttl_seconds=60,
        now=datetime(2026, 9, 12, 10, 2, tzinfo=timezone.utc),
    )
    assert quarantined == 0
    normalized = [statement.upper() for statement in statements]
    assert any("SELECT 1" in statement for statement in normalized)
    assert not any(
        statement.lstrip().startswith("UPDATE PROVIDER_DISPATCH_OUTBOX")
        for statement in normalized
    )

def test_event_safe_outbox_does_not_hook_generic_claim_path() -> None:
    # Event maintenance must not run inside every provider claim. Partner, booking,
    # lesson and other workers share this repository and require their native
    # PostgreSQL claim concurrency semantics unchanged.
    assert "claim_due" not in EventSafeDispatchOutboxRepository.__dict__



def test_business_switch_off_blocks_commercial_event_at_provider_boundary() -> None:
    conn = _db(); item = _item()
    conn.execute("UPDATE clientplatform_event_followup_settings SET enabled=0,settings_epoch=2")
    assert not event_commercial_claim_can_cross_provider_boundary(
        conn, item, now='2026-09-12T10:01:00+00:00'
    )
    row = conn.execute(
        "SELECT status,last_error FROM provider_dispatch_outbox WHERE id='d'"
    ).fetchone()
    assert tuple(row) == ('cancelled','event_commercial_business_disabled')


def test_commercial_event_policy_is_rechecked_at_provider_boundary() -> None:
    conn = _db(); item = _item()
    with patch(
        "clientplatform.infrastructure.event_dispatch_safety.event_commercial_policy_authorized",
        return_value=False,
    ):
        assert not event_commercial_claim_can_cross_provider_boundary(
            conn, item, now='2026-09-12T10:01:00+00:00'
        )
    row = conn.execute(
        "SELECT status,last_error FROM provider_dispatch_outbox WHERE id='d'"
    ).fetchone()
    assert tuple(row) == ('cancelled','event_commercial_policy_not_authorized')


def test_legacy_offer_email_is_suppressed_at_provider_boundary() -> None:
    conn = _db(); item = _item()
    legacy_key = 'event:e:registration:r:message:after:v2'
    conn.execute(
        "UPDATE provider_dispatch_outbox SET idempotency_key=? WHERE id='d'",
        (legacy_key,),
    )
    legacy_item = ClaimedProviderDispatch(
        dispatch=replace(item.dispatch, idempotency_key=legacy_key),
        external_subject=item.external_subject,
        credential_reference=item.credential_reference,
    )
    assert not EventSafeDispatchOutboxRepository(conn).event_message_claim_can_cross_provider_boundary(
        legacy_item, now='2026-09-12T10:01:00+00:00'
    )
    row = conn.execute(
        "SELECT status,last_error FROM provider_dispatch_outbox WHERE id='d'"
    ).fetchone()
    assert tuple(row) == ('cancelled','legacy_event_offer_suppressed')


def test_disabled_channel_after_claim_blocks_provider_write() -> None:
    conn = _db(); item = _item()
    conn.execute("UPDATE clientplatform_event_followup_settings SET channel_email=0")
    assert not event_commercial_claim_can_cross_provider_boundary(
        conn, item, now='2026-09-12T10:01:00+00:00'
    )
    row = conn.execute(
        "SELECT status,last_error FROM provider_dispatch_outbox WHERE id='d'"
    ).fetchone()
    assert tuple(row) == ('cancelled','event_commercial_strategy_disabled')


def test_disabled_participant_group_after_claim_blocks_non_replay_boundary() -> None:
    conn = _db(); item = _item()
    conn.execute("UPDATE clientplatform_event_followup_settings SET segment_no_show=0")
    assert not mark_event_commercial_non_replay_boundary(
        conn, item, now='2026-09-12T10:00:01+00:00'
    )
    row = conn.execute(
        "SELECT status,last_error FROM provider_dispatch_outbox WHERE id='d'"
    ).fetchone()
    assert tuple(row) == ('cancelled','event_commercial_strategy_disabled')

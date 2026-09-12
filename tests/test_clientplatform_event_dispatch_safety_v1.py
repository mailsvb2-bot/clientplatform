from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from clientplatform.domain.connections import ConnectionPlatform, DispatchStatus
from clientplatform.domain.programs import ContentKind
from clientplatform.infrastructure.event_dispatch_safety import (
    event_commercial_claim_can_cross_provider_boundary,
    mark_event_commercial_non_replay_boundary,
    quarantine_stale_event_commercial_boundaries,
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
          id TEXT,business_id TEXT,event_id TEXT,status TEXT,email TEXT,customer_id TEXT
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
        """
    )
    conn.execute("INSERT INTO businesses VALUES('b','active')")
    conn.execute("INSERT INTO clientplatform_events VALUES('e','b','completed')")
    conn.execute("INSERT INTO clientplatform_event_registrations VALUES('r','b','e','registered','a@example.test',NULL)")
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
    assert event_commercial_claim_can_cross_provider_boundary(conn, item)
    conn.execute("INSERT INTO clientplatform_event_conversion_links VALUES('b','e','r')")
    assert not event_commercial_claim_can_cross_provider_boundary(conn, item, now='2026-09-12T10:01:00+00:00')
    row = conn.execute("SELECT status,last_error FROM provider_dispatch_outbox WHERE id='d'").fetchone()
    assert tuple(row) == ('cancelled','event_message_authority_revoked_or_paid')


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

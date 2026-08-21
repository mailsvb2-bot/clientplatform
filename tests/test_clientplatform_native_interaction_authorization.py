from __future__ import annotations

import sqlite3
from dataclasses import replace
from uuid import uuid4

from clientplatform.domain.connections import (
    ConnectionPlatform,
    DispatchStatus,
)
from clientplatform.domain.programs import ContentKind
from clientplatform.infrastructure.safe_member_dispatch_outbox import (
    DispatchOutboxRepository,
)
from clientplatform.infrastructure.unified_dispatch_outbox import (
    ClaimedProviderDispatch,
    ProviderDispatch,
)


_STAMP = "2026-08-21T05:00:00+00:00"


def _provider_dispatch(
    *,
    business_id: str,
    connection_id: str,
    external_subject: str,
    source_kind: str,
) -> ProviderDispatch:
    return ProviderDispatch(
        id=str(uuid4()),
        business_id=business_id,
        platform=ConnectionPlatform.VK,
        source_kind=source_kind,
        source_id=f"{source_kind}:test",
        connection_id=connection_id,
        external_subject=external_subject,
        payload_kind=ContentKind.MIXED,
        payload_ref='{"schema_version":1,"text":"menu","rows":[]}',
        idempotency_key=f"{source_kind}:test",
        status=DispatchStatus.SENDING,
        attempts=0,
        available_at=_STAMP,
        created_at=_STAMP,
        updated_at=_STAMP,
        locked_at=_STAMP,
        lock_token="lease-1",
    )


def _base_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE businesses(id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE connections(
            id TEXT PRIMARY KEY, business_id TEXT NOT NULL,
            platform TEXT NOT NULL, status TEXT NOT NULL
        );
        CREATE TABLE accounts(account_id INTEGER PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE account_channel_identities(
            account_id INTEGER NOT NULL, platform TEXT NOT NULL,
            external_user_id TEXT NOT NULL
        );
        CREATE TABLE business_members(
            id TEXT PRIMARY KEY, business_id TEXT NOT NULL,
            user_id INTEGER NOT NULL, status TEXT NOT NULL
        );
        CREATE TABLE customers(
            id TEXT PRIMARY KEY, business_id TEXT NOT NULL, status TEXT NOT NULL
        );
        CREATE TABLE customer_identities(
            id TEXT PRIMARY KEY, business_id TEXT NOT NULL,
            customer_id TEXT NOT NULL, platform TEXT NOT NULL,
            external_subject TEXT NOT NULL, status TEXT NOT NULL
        );
        CREATE TABLE provider_dispatch_outbox(
            id TEXT PRIMARY KEY, business_id TEXT NOT NULL,
            platform TEXT NOT NULL, source_kind TEXT NOT NULL,
            source_id TEXT NOT NULL, connection_id TEXT NOT NULL,
            customer_identity_id TEXT, external_subject TEXT NOT NULL,
            status TEXT NOT NULL, lock_token TEXT, locked_at TEXT,
            updated_at TEXT NOT NULL, last_error TEXT
        );
        """
    )


def _insert_claim(
    conn: sqlite3.Connection,
    dispatch: ProviderDispatch,
    *,
    customer_identity_id: str | None = None,
) -> ClaimedProviderDispatch:
    conn.execute(
        """
        INSERT INTO provider_dispatch_outbox(
            id,business_id,platform,source_kind,source_id,connection_id,
            customer_identity_id,external_subject,status,lock_token,locked_at,
            updated_at,last_error
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)
        """,
        (
            dispatch.id,
            dispatch.business_id,
            dispatch.platform.value,
            dispatch.source_kind,
            dispatch.source_id,
            dispatch.connection_id,
            customer_identity_id,
            dispatch.external_subject,
            "sending",
            dispatch.lock_token,
            dispatch.locked_at,
            _STAMP,
        ),
    )
    return ClaimedProviderDispatch(
        dispatch=dispatch,
        external_subject=dispatch.external_subject,
        credential_reference="secret://test/provider",
    )


def test_revoked_member_is_cancelled_before_provider_boundary() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _base_schema(conn)
    business_id = str(uuid4())
    connection_id = str(uuid4())
    member_id = 101
    subject = "700001"
    conn.execute("INSERT INTO businesses VALUES(?, 'active')", (business_id,))
    conn.execute(
        "INSERT INTO connections VALUES(?,?,'vk','active')",
        (connection_id, business_id),
    )
    conn.execute("INSERT INTO accounts VALUES(?, 'active')", (member_id,))
    conn.execute(
        "INSERT INTO account_channel_identities VALUES(?,'vk',?)",
        (member_id, subject),
    )
    conn.execute(
        "INSERT INTO business_members VALUES(?,?,?,'revoked')",
        (str(uuid4()), business_id, member_id),
    )
    dispatch = _provider_dispatch(
        business_id=business_id,
        connection_id=connection_id,
        external_subject=subject,
        source_kind="member_interaction",
    )
    claim = _insert_claim(conn, dispatch)

    assert not DispatchOutboxRepository(conn).native_interaction_claim_can_cross_provider_boundary(
        claim, now="2026-08-21T05:00:01+00:00"
    )
    row = conn.execute(
        "SELECT status,last_error FROM provider_dispatch_outbox WHERE id=?",
        (dispatch.id,),
    ).fetchone()
    assert row["status"] == "cancelled"
    assert row["last_error"] == "member_interaction_recipient_revoked"
    conn.close()


def test_revoked_customer_identity_is_cancelled_before_provider_boundary() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _base_schema(conn)
    business_id = str(uuid4())
    connection_id = str(uuid4())
    customer_id = str(uuid4())
    identity_id = str(uuid4())
    subject = "700002"
    conn.execute("INSERT INTO businesses VALUES(?, 'active')", (business_id,))
    conn.execute(
        "INSERT INTO connections VALUES(?,?,'vk','active')",
        (connection_id, business_id),
    )
    conn.execute(
        "INSERT INTO customers VALUES(?,?,'active')",
        (customer_id, business_id),
    )
    conn.execute(
        "INSERT INTO customer_identities VALUES(?,?,?,'vk',?,'revoked')",
        (identity_id, business_id, customer_id, subject),
    )
    dispatch = _provider_dispatch(
        business_id=business_id,
        connection_id=connection_id,
        external_subject=subject,
        source_kind="customer_interaction",
    )
    claim = _insert_claim(conn, dispatch, customer_identity_id=identity_id)

    assert not DispatchOutboxRepository(conn).native_interaction_claim_can_cross_provider_boundary(
        claim, now="2026-08-21T05:00:01+00:00"
    )
    row = conn.execute(
        "SELECT status,last_error FROM provider_dispatch_outbox WHERE id=?",
        (dispatch.id,),
    ).fetchone()
    assert row["status"] == "cancelled"
    assert row["last_error"] == "customer_interaction_recipient_revoked"
    conn.close()

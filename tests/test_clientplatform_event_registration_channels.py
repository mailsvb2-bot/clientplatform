from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import unittest

from clientplatform.application.event_commercial_consent import (
    active_event_commercial_channels,
    commercial_consent_text_sha256,
)
from clientplatform.application.event_registration_channels import (
    EventRegistrationChannelLinkRejected,
    consume_event_registration_channel_link_in_transaction,
    extract_event_registration_channel_link_token,
    issue_event_registration_channel_links_in_transaction,
)
from clientplatform.application.events import (
    create_event_in_transaction,
    publish_event_in_transaction,
    register_public_attendee_in_transaction,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import create_or_update_tables


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    create_or_update_tables(conn)
    return conn


def _published_registration(conn: sqlite3.Connection, *, user_id: int = 990):
    access = TenancyRepository(conn).create_business(
        owner_user_id=user_id,
        name="Scoped Webinar Business",
    )
    actor = TenancyRepository(conn).resolve_context(
        user_id=user_id,
        business_id=access.business.id,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    event = create_event_in_transaction(
        conn,
        actor=actor,
        title="Scoped webinar",
        description="Registration-scoped channel verification",
        starts_at=now + timedelta(days=2),
        timezone_name="Europe/Moscow",
        join_url="https://stream.example.test/live",
        now=now,
    )
    event = publish_event_in_transaction(
        conn,
        actor=actor,
        event_id=event.id,
        now=now,
    )
    result = register_public_attendee_in_transaction(
        conn,
        public_slug=event.public_slug,
        name="Participant",
        email=f"participant-{user_id}@example.test",
        consent=True,
        now=now,
    )
    return actor, event, result


def _telegram_connection(conn: sqlite3.Connection, actor) -> str:
    connection_id = str(uuid4())
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conn.execute(
        """
        INSERT INTO connections(
            id,business_id,platform,connection_type,external_account_id,
            credential_reference,permissions_json,status,created_by_member_id,
            created_at,updated_at,last_success_at,last_error_at,last_error_code
        ) VALUES(?,?,'telegram','telegram_shared_bot','shared-clientplatform',
                 'secret://env/TEST_TELEGRAM_TOKEN','[]','active',?,?,?,NULL,NULL,NULL)
        """,
        (
            connection_id,
            actor.business_id,
            actor.membership_id,
            now,
            now,
        ),
    )
    return connection_id


def test_event_channel_verification_is_registration_scoped_and_does_not_capture_crm_identity() -> None:
    conn = _conn()
    actor, event, result = _published_registration(conn)
    telegram_connection = _telegram_connection(conn, actor)
    consent_hash = commercial_consent_text_sha256("Scoped Webinar Business")

    links = issue_event_registration_channel_links_in_transaction(
        conn,
        registration=result.registration,
        marketing_platforms=("telegram",),
        expected_marketing_text_sha256=consent_hash,
    )
    telegram = next(item for item in links if item.platform == "telegram")
    assert telegram.marketing_requested is True
    assert conn.execute(
        "SELECT COUNT(*) FROM customer_channel_link_tokens"
    ).fetchone()[0] == 0
    assert active_event_commercial_channels(
        conn,
        business_id=actor.business_id,
        event_id=event.id,
        registration_id=result.registration.id,
    ) == ()

    with patch(
        "clientplatform.application.event_notifications._public_base_url",
        return_value="https://clientplatform.example.test",
    ):
        verified = consume_event_registration_channel_link_in_transaction(
            conn,
            token=telegram.token,
            platform="telegram",
            external_subject="100200300",
            expected_business_id=actor.business_id,
            connection_id=telegram_connection,
        )

    assert verified.marketing_consent_recorded is True
    assert verified.notifications_queued == 4
    assert active_event_commercial_channels(
        conn,
        business_id=actor.business_id,
        event_id=event.id,
        registration_id=result.registration.id,
    ) == ("telegram",)

    scoped = conn.execute(
        """
        SELECT platform,external_subject,connection_id
        FROM clientplatform_event_registration_channels
        WHERE business_id=? AND event_id=? AND registration_id=?
        """,
        (actor.business_id, event.id, result.registration.id),
    ).fetchone()
    assert tuple(scoped) == ("telegram", "100200300", telegram_connection)

    assert conn.execute(
        """
        SELECT COUNT(*)
        FROM customer_identities
        WHERE business_id=? AND customer_id=? AND platform='telegram'
        """,
        (actor.business_id, result.registration.customer_id),
    ).fetchone()[0] == 0

    queued = conn.execute(
        """
        SELECT recipient_kind,customer_identity_id,external_subject,connection_id
        FROM provider_dispatch_outbox
        WHERE business_id=? AND source_id=? AND platform='telegram'
        ORDER BY available_at,id
        """,
        (actor.business_id, result.registration.id),
    ).fetchall()
    assert len(queued) == 4
    assert {
        (
            row["recipient_kind"],
            row["customer_identity_id"],
            row["external_subject"],
            row["connection_id"],
        )
        for row in queued
    } == {("external_subject", None, "100200300", telegram_connection)}
    conn.close()


def test_second_messenger_account_cannot_hijack_verified_registration() -> None:
    conn = _conn()
    actor, _event, result = _published_registration(conn, user_id=991)
    first_links = issue_event_registration_channel_links_in_transaction(
        conn,
        registration=result.registration,
    )
    first = next(item for item in first_links if item.platform == "telegram")
    consume_event_registration_channel_link_in_transaction(
        conn,
        token=first.token,
        platform="telegram",
        external_subject="111111",
        expected_business_id=actor.business_id,
    )

    second_links = issue_event_registration_channel_links_in_transaction(
        conn,
        registration=result.registration,
    )
    second = next(item for item in second_links if item.platform == "telegram")
    with unittest.TestCase().assertRaisesRegex(
        EventRegistrationChannelLinkRejected,
        "already verified to another messenger account",
    ):
        consume_event_registration_channel_link_in_transaction(
            conn,
            token=second.token,
            platform="telegram",
            external_subject="222222",
            expected_business_id=actor.business_id,
        )
    digest_row = conn.execute(
        """
        SELECT consumed_at
        FROM clientplatform_event_channel_link_tokens
        WHERE target_platform='telegram'
        ORDER BY created_at DESC,id DESC
        LIMIT 1
        """
    ).fetchone()
    assert digest_row["consumed_at"] is None
    conn.close()


def test_event_channel_token_is_business_bound_single_use_and_parseable() -> None:
    conn = _conn()
    actor, _event, result = _published_registration(conn, user_id=992)
    links = issue_event_registration_channel_links_in_transaction(
        conn,
        registration=result.registration,
    )
    vk = next(item for item in links if item.platform == "vk")
    assert extract_event_registration_channel_link_token(
        f"/start ecv_{vk.token}"
    ) == vk.token
    assert extract_event_registration_channel_link_token(f"ecv_{vk.token}") == vk.token
    assert extract_event_registration_channel_link_token("cplink_other") is None

    with unittest.TestCase().assertRaisesRegex(
        EventRegistrationChannelLinkRejected,
        "another business",
    ):
        consume_event_registration_channel_link_in_transaction(
            conn,
            token=vk.token,
            platform="vk",
            external_subject="123456",
            expected_business_id=str(uuid4()),
        )

    consume_event_registration_channel_link_in_transaction(
        conn,
        token=vk.token,
        platform="vk",
        external_subject="123456",
        expected_business_id=actor.business_id,
    )
    with unittest.TestCase().assertRaisesRegex(
        EventRegistrationChannelLinkRejected,
        "already consumed",
    ):
        consume_event_registration_channel_link_in_transaction(
            conn,
            token=vk.token,
            platform="vk",
            external_subject="123456",
            expected_business_id=actor.business_id,
        )
    conn.close()

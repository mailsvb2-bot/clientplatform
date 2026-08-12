from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from clientplatform.domain.promotions import (
    PromotionChannel,
    PromotionCreative,
    PromotionEventType,
    PromotionInvariantViolation,
    rewrite_promotion_source_url,
    stable_creative_id,
)
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.infrastructure.promotion_repository import PromotionRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_bookings,
    clientplatform_customers,
    clientplatform_promotions,
    clientplatform_tenancy,
)


def _repository_fixture():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    clientplatform_tenancy.ensure(conn)
    clientplatform_customers.ensure(conn)
    clientplatform_activity.ensure(conn)
    clientplatform_bookings.ensure(conn)
    clientplatform_promotions.ensure(conn)
    tenancy = TenancyRepository(conn)
    activity = ActivityRepository(conn)
    bookings = BookingRepository(conn)
    promotions = PromotionRepository(conn)
    business = tenancy.create_business(owner_user_id=101, name="Source Lab")
    owner = tenancy.resolve_context(user_id=101, business_id=business.business.id)
    activity.upsert_profile(
        actor=owner,
        activity_description="Консультации",
        timezone_name="Europe/Amsterdam",
        now="2026-08-01T10:00:00+00:00",
    )
    capability = activity.enable_capability(
        actor=owner,
        connector_key="services",
        now="2026-08-01T10:00:00+00:00",
    )
    offering = activity.create_offering(
        actor=owner,
        capability_id=capability.id,
        title="Первая консультация",
        description="Знакомство и обсуждение задачи",
        now="2026-08-01T10:00:00+00:00",
    )
    slot = bookings.create_slot(
        actor=owner,
        offering_id=offering.id,
        local_start="20.08.2026 12:00",
        duration_minutes=60,
        now="2026-08-01T10:00:00+00:00",
    )
    creative = PromotionCreative(
        creative_id=stable_creative_id("source-lab"),
        headline="Свободное время",
        primary_text="Можно записаться на консультацию.",
        description="60 минут",
    )
    campaign, _ = promotions.create_or_refresh_campaign(
        actor=owner,
        slot_id=slot.slot.id,
        channel=PromotionChannel.WEBSITE,
        creative=creative,
        now="2026-08-01T10:10:00+00:00",
    )
    issued = activity.issue_customer_invite(
        actor=owner,
        now="2026-08-01T10:11:00+00:00",
    )
    customer = activity.claim_customer_invite(
        token=issued.token,
        telegram_user_id=700001,
        username="source-customer",
        display_name="Клиент",
        now="2026-08-01T10:12:00+00:00",
    )
    return conn, owner, promotions, campaign, customer.customer_id


def test_source_alias_is_stable_public_and_event_specific() -> None:
    conn, owner, promotions, campaign, customer_id = _repository_fixture()
    alias_a = promotions.ensure_source_alias(
        actor=owner,
        campaign_id=campaign.id,
        source_kind="creative_variant",
        source_key="trial-a:variant-a",
        now="2026-08-01T10:15:00+00:00",
    )
    alias_a_again = promotions.ensure_source_alias(
        actor=owner,
        campaign_id=campaign.id,
        source_kind="creative_variant",
        source_key="trial-a:variant-a",
        now="2026-08-01T10:16:00+00:00",
    )
    alias_b = promotions.ensure_source_alias(
        actor=owner,
        campaign_id=campaign.id,
        source_kind="creative_variant",
        source_key="trial-a:variant-b",
        now="2026-08-01T10:17:00+00:00",
    )
    assert alias_a.source_token == alias_a_again.source_token
    assert alias_a.source_token != alias_b.source_token

    resolved = promotions.resolve_public_source(
        source_token=alias_a.source_token,
        now="2026-08-01T10:20:00+00:00",
    )
    assert resolved.campaign.id == campaign.id
    assert resolved.attribution_token == alias_a.source_token
    assert resolved.source_kind == "creative_variant"

    assert promotions.record_event(
        campaign=campaign,
        customer_id=customer_id,
        event_type=PromotionEventType.OPENED,
        source_token=alias_a.source_token,
        now="2026-08-01T10:21:00+00:00",
    )
    assert not promotions.record_event(
        campaign=campaign,
        customer_id=customer_id,
        event_type=PromotionEventType.OPENED,
        source_token=alias_a.source_token,
        now="2026-08-01T10:22:00+00:00",
    )
    assert promotions.record_event(
        campaign=campaign,
        customer_id=customer_id,
        event_type=PromotionEventType.OPENED,
        source_token=alias_b.source_token,
        now="2026-08-01T10:23:00+00:00",
    )
    rows = conn.execute(
        "SELECT source_token FROM promotion_events ORDER BY occurred_at"
    ).fetchall()
    assert [row[0] for row in rows] == [alias_a.source_token, alias_b.source_token]


def test_event_rejects_alias_owned_by_another_campaign() -> None:
    conn, owner, promotions, campaign, customer_id = _repository_fixture()
    creative = PromotionCreative(
        creative_id=stable_creative_id("source-lab-vk"),
        headline="Свободное время",
        primary_text="Запись через другой канал.",
        description="60 минут",
    )
    other, _ = promotions.create_or_refresh_campaign(
        actor=owner,
        slot_id=campaign.booking_slot_id,
        channel=PromotionChannel.VK,
        creative=creative,
        now="2026-08-01T10:18:00+00:00",
    )
    wrong_alias = promotions.ensure_source_alias(
        actor=owner,
        campaign_id=other.id,
        source_kind="creative_variant",
        source_key="trial-b:variant-b",
        now="2026-08-01T10:19:00+00:00",
    )
    with pytest.raises(PromotionInvariantViolation, match="does not belong"):
        promotions.record_event(
            campaign=campaign,
            customer_id=customer_id,
            event_type=PromotionEventType.OPENED,
            source_token=wrong_alias.source_token,
            now="2026-08-01T10:20:00+00:00",
        )
    assert conn.execute("SELECT COUNT(*) FROM promotion_events").fetchone()[0] == 0


def test_rewrite_promotion_source_url_changes_only_expected_start_payload() -> None:
    rewritten = rewrite_promotion_source_url(
        "https://t.me/clientplatformbot?start=cpa_campaignBase12&ref=owner",
        from_token="campaignBase12",
        to_token="variantSource12",
    )
    assert rewritten == "https://t.me/clientplatformbot?start=cpa_variantSource12&ref=owner"
    with pytest.raises(PromotionInvariantViolation, match="expected promotion source"):
        rewrite_promotion_source_url(
            rewritten,
            from_token="campaignBase12",
            to_token="anotherSource12",
        )


def test_schema_migration_backfills_legacy_event_source_token() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE promotion_campaigns(
            id TEXT PRIMARY KEY, business_id TEXT NOT NULL, offering_id TEXT NOT NULL,
            booking_slot_id TEXT NOT NULL, channel TEXT NOT NULL,
            source_token TEXT NOT NULL UNIQUE, creative_id TEXT NOT NULL,
            headline TEXT NOT NULL, primary_text TEXT NOT NULL,
            description TEXT NOT NULL, cta TEXT NOT NULL,
            creative_style TEXT NOT NULL, status TEXT NOT NULL,
            created_by_member_id TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, UNIQUE(id, business_id),
            UNIQUE(business_id, booking_slot_id, channel)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE promotion_events(
            id TEXT PRIMARY KEY, business_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
            event_type TEXT NOT NULL, customer_id TEXT NOT NULL,
            booking_slot_id TEXT NOT NULL, dedupe_key TEXT NOT NULL,
            occurred_at TEXT NOT NULL, UNIQUE(id, business_id),
            UNIQUE(business_id, dedupe_key)
        )
        """
    )
    campaign_id = str(uuid4())
    business_id = str(uuid4())
    source_token = "legacySource123"
    conn.execute(
        """
        INSERT INTO promotion_campaigns(
            id, business_id, offering_id, booking_slot_id, channel,
            source_token, creative_id, headline, primary_text, description,
            cta, creative_style, status, created_by_member_id, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            campaign_id,
            business_id,
            str(uuid4()),
            str(uuid4()),
            "website",
            source_token,
            stable_creative_id("legacy"),
            "Headline",
            "Primary",
            "Description",
            "Записаться",
            "direct",
            "active",
            str(uuid4()),
            "2026-08-01T10:00:00+00:00",
            "2026-08-01T10:00:00+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO promotion_events VALUES(?, ?, ?, 'opened', ?, ?, ?, ?)",
        (
            str(uuid4()),
            business_id,
            campaign_id,
            str(uuid4()),
            str(uuid4()),
            "legacy-dedupe",
            "2026-08-01T10:05:00+00:00",
        ),
    )

    clientplatform_promotions.ensure(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(promotion_events)")}
    assert "source_token" in columns
    row = conn.execute("SELECT source_token FROM promotion_events").fetchone()
    assert row is not None and row[0] == source_token
    assert conn.execute("SELECT COUNT(*) FROM promotion_source_aliases").fetchone()[0] == 0

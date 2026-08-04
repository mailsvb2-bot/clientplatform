from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.bookings import (
    BookingSlot,
    BookingSlotStatus,
    BookingSlotView,
)
from clientplatform.domain.promotions import (
    PromotionCampaign,
    PromotionCampaignStatus,
    PromotionChannel,
    PromotionCreative,
    PromotionEventType,
    PromotionInvariantViolation,
    PromotionNotFound,
    PromotionStats,
    new_source_token,
    normalize_source_token,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _campaign_from_row(row: Any) -> PromotionCampaign:
    return PromotionCampaign(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        offering_id=str(_value(row, "offering_id", 2)),
        booking_slot_id=str(_value(row, "booking_slot_id", 3)),
        channel=PromotionChannel(str(_value(row, "channel", 4))),
        source_token=str(_value(row, "source_token", 5)),
        creative=PromotionCreative(
            creative_id=str(_value(row, "creative_id", 6)),
            headline=str(_value(row, "headline", 7)),
            primary_text=str(_value(row, "primary_text", 8)),
            description=str(_value(row, "description", 9)),
            cta=str(_value(row, "cta", 10)),
            style=str(_value(row, "creative_style", 11)),
        ),
        status=PromotionCampaignStatus(str(_value(row, "status", 12))),
        created_by_member_id=str(_value(row, "created_by_member_id", 13)),
        created_at=str(_value(row, "created_at", 14)),
        updated_at=str(_value(row, "updated_at", 15)),
    )


def _slot_view_from_row(row: Any) -> BookingSlotView:
    booked_customer_id = _value(row, "booked_customer_id", 7)
    booked_at = _value(row, "booked_at", 11)
    cancelled_at = _value(row, "cancelled_at", 12)
    slot = BookingSlot(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        offering_id=str(_value(row, "offering_id", 2)),
        starts_at=str(_value(row, "starts_at", 3)),
        ends_at=str(_value(row, "ends_at", 4)),
        duration_minutes=int(_value(row, "duration_minutes", 5)),
        status=BookingSlotStatus(str(_value(row, "status", 6))),
        booked_customer_id=(
            None if booked_customer_id is None else str(booked_customer_id)
        ),
        created_by_member_id=str(_value(row, "created_by_member_id", 8)),
        created_at=str(_value(row, "created_at", 9)),
        updated_at=str(_value(row, "updated_at", 10)),
        booked_at=None if booked_at is None else str(booked_at),
        cancelled_at=None if cancelled_at is None else str(cancelled_at),
    )
    return BookingSlotView(
        slot=slot,
        offering_title=str(_value(row, "offering_title", 13)),
        business_name=str(_value(row, "business_name", 14)),
        timezone=str(_value(row, "timezone", 15)),
    )


_CAMPAIGN_SELECT = """
    SELECT id, business_id, offering_id, booking_slot_id, channel,
           source_token, creative_id, headline, primary_text, description,
           cta, creative_style, status, created_by_member_id, created_at,
           updated_at
    FROM promotion_campaigns
"""


class PromotionRepository:
    """Tenant-safe campaigns with idempotent, restart-safe outcome evidence."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)
        self._bookings = BookingRepository(conn)

    def _current_actor(self, actor: TenantContext) -> TenantContext:
        return self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )

    def create_or_refresh_campaign(
        self,
        *,
        actor: TenantContext,
        slot_id: str,
        channel: PromotionChannel | str,
        creative: PromotionCreative,
        now: str | None = None,
    ) -> tuple[PromotionCampaign, BookingSlotView]:
        current = self._current_actor(actor)
        current.assert_can_manage_promotions()
        slot = self._bookings.get_slot(actor=current, slot_id=slot_id)
        if slot.slot.status != BookingSlotStatus.OPEN:
            raise PromotionInvariantViolation(
                "Рекламировать можно только свободное опубликованное время"
            )
        selected_channel = (
            channel if isinstance(channel, PromotionChannel) else PromotionChannel(str(channel))
        )
        timestamp = str(now or _utc_now())
        campaign_id = str(uuid4())
        source_token = new_source_token()
        self._conn.execute(
            """
            INSERT INTO promotion_campaigns(
                id, business_id, offering_id, booking_slot_id, channel,
                source_token, creative_id, headline, primary_text,
                description, cta, creative_style, status,
                created_by_member_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            ON CONFLICT(business_id, booking_slot_id, channel) DO UPDATE SET
                creative_id=excluded.creative_id,
                headline=excluded.headline,
                primary_text=excluded.primary_text,
                description=excluded.description,
                cta=excluded.cta,
                creative_style=excluded.creative_style,
                status='active',
                updated_at=excluded.updated_at
            """,
            (
                campaign_id,
                current.business_id,
                slot.slot.offering_id,
                slot.slot.id,
                selected_channel.value,
                source_token,
                creative.creative_id,
                creative.headline,
                creative.primary_text,
                creative.description,
                creative.cta,
                creative.style,
                current.membership_id,
                timestamp,
                timestamp,
            ),
        )
        campaign = self.get_campaign_for_slot(
            actor=current,
            slot_id=slot.slot.id,
            channel=selected_channel,
        )
        return campaign, slot

    def get_campaign(
        self,
        *,
        actor: TenantContext,
        campaign_id: str,
    ) -> PromotionCampaign:
        current = self._current_actor(actor)
        current.assert_can_view_promotion_analytics()
        normalized_id = normalize_uuid(campaign_id, field_name="campaign_id")
        row = self._conn.execute(
            _CAMPAIGN_SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (normalized_id, current.business_id),
        ).fetchone()
        if row is None:
            raise PromotionNotFound("Рекламная кампания не найдена")
        return _campaign_from_row(row)

    def get_campaign_for_slot(
        self,
        *,
        actor: TenantContext,
        slot_id: str,
        channel: PromotionChannel | str,
    ) -> PromotionCampaign:
        current = self._current_actor(actor)
        current.assert_can_manage_promotions()
        normalized_slot = normalize_uuid(slot_id, field_name="booking_slot_id")
        selected_channel = (
            channel if isinstance(channel, PromotionChannel) else PromotionChannel(str(channel))
        )
        row = self._conn.execute(
            _CAMPAIGN_SELECT
            + " WHERE business_id=? AND booking_slot_id=? AND channel=? LIMIT 1",
            (current.business_id, normalized_slot, selected_channel.value),
        ).fetchone()
        if row is None:
            raise PromotionNotFound("Рекламная кампания для этого времени не найдена")
        return _campaign_from_row(row)

    def list_campaigns(self, *, actor: TenantContext) -> list[PromotionCampaign]:
        current = self._current_actor(actor)
        current.assert_can_view_promotion_analytics()
        rows = self._conn.execute(
            _CAMPAIGN_SELECT
            + " WHERE business_id=? ORDER BY created_at DESC, id DESC",
            (current.business_id,),
        ).fetchall()
        return [_campaign_from_row(row) for row in rows]

    def get_public_campaign(self, *, source_token: str) -> PromotionCampaign:
        token = normalize_source_token(source_token)
        row = self._conn.execute(
            _CAMPAIGN_SELECT
            + """
              WHERE source_token=? AND status='active'
                AND EXISTS(
                    SELECT 1
                    FROM booking_slots bs
                    WHERE bs.id=promotion_campaigns.booking_slot_id
                      AND bs.business_id=promotion_campaigns.business_id
                      AND bs.status='open'
                )
              LIMIT 1
            """,
            (token,),
        ).fetchone()
        if row is None:
            raise PromotionNotFound(
                "Эта рекламная ссылка больше не активна. Откройте страницу специалиста заново."
            )
        return _campaign_from_row(row)

    def get_public_campaign_slot(self, *, campaign: PromotionCampaign) -> BookingSlotView:
        row = self._conn.execute(
            """
            SELECT bs.id, bs.business_id, bs.offering_id, bs.starts_at, bs.ends_at,
                   bs.duration_minutes, bs.status, bs.booked_customer_id,
                   bs.created_by_member_id, bs.created_at, bs.updated_at,
                   bs.booked_at, bs.cancelled_at,
                   bo.title AS offering_title, b.name AS business_name,
                   bp.timezone AS timezone
            FROM booking_slots bs
            JOIN business_offerings bo
              ON bo.id=bs.offering_id AND bo.business_id=bs.business_id
            JOIN businesses b
              ON b.id=bs.business_id AND b.status='active'
            JOIN business_profiles bp
              ON bp.business_id=bs.business_id
            WHERE bs.id=? AND bs.business_id=?
            LIMIT 1
            """,
            (campaign.booking_slot_id, campaign.business_id),
        ).fetchone()
        if row is None:
            raise PromotionNotFound("Опубликованное время больше не найдено")
        return _slot_view_from_row(row)

    def record_event(
        self,
        *,
        campaign: PromotionCampaign,
        customer_id: str,
        event_type: PromotionEventType | str,
        now: str | None = None,
    ) -> bool:
        normalized_customer = normalize_uuid(customer_id, field_name="customer_id")
        selected_type = (
            event_type
            if isinstance(event_type, PromotionEventType)
            else PromotionEventType(str(event_type))
        )
        timestamp = str(now or _utc_now())
        dedupe_key = f"{selected_type.value}:{campaign.id}:{normalized_customer}"
        cursor = self._conn.execute(
            """
            INSERT INTO promotion_events(
                id, business_id, campaign_id, event_type, customer_id,
                booking_slot_id, dedupe_key, occurred_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(business_id, dedupe_key) DO NOTHING
            """,
            (
                str(uuid4()),
                campaign.business_id,
                campaign.id,
                selected_type.value,
                normalized_customer,
                campaign.booking_slot_id,
                dedupe_key,
                timestamp,
            ),
        )
        inserted = int(getattr(cursor, "rowcount", 1) or 0) == 1
        if selected_type == PromotionEventType.BOOKED:
            self._conn.execute(
                """
                UPDATE promotion_campaigns
                SET status='closed', updated_at=?
                WHERE id=? AND business_id=?
                """,
                (timestamp, campaign.id, campaign.business_id),
            )
        return inserted

    def stats(
        self,
        *,
        actor: TenantContext,
        campaign_id: str | None = None,
    ) -> PromotionStats:
        current = self._current_actor(actor)
        current.assert_can_view_promotion_analytics()
        params: list[Any] = [current.business_id]
        campaign_filter = ""
        if campaign_id is not None:
            normalized_campaign = normalize_uuid(campaign_id, field_name="campaign_id")
            self.get_campaign(actor=current, campaign_id=normalized_campaign)
            campaign_filter = " AND pc.id=?"
            params.append(normalized_campaign)
        row = self._conn.execute(
            """
            SELECT COUNT(DISTINCT pc.id) AS campaigns,
                   COUNT(DISTINCT CASE WHEN pe.event_type='opened'
                                       THEN pe.customer_id END) AS people_opened,
                   COUNT(DISTINCT CASE WHEN pe.event_type='booked'
                                       THEN pe.customer_id END) AS bookings
            FROM promotion_campaigns pc
            LEFT JOIN promotion_events pe
              ON pe.business_id=pc.business_id AND pe.campaign_id=pc.id
            WHERE pc.business_id=?
            """
            + campaign_filter,
            tuple(params),
        ).fetchone()
        if row is None:
            return PromotionStats(campaigns=0, people_opened=0, bookings=0)
        return PromotionStats(
            campaigns=int(_value(row, "campaigns", 0) or 0),
            people_opened=int(_value(row, "people_opened", 1) or 0),
            bookings=int(_value(row, "bookings", 2) or 0),
        )


__all__ = ["PromotionRepository"]

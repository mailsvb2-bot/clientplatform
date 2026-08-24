from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.application.customer_role_guard import (
    active_member_business_ids,
    assert_external_customer,
)
from clientplatform.domain.bookings import (
    BookingClaim,
    BookingNotFound,
    BookingSlotView,
    CustomerBusinessLink,
    normalize_utc_datetime,
)
from clientplatform.domain.outcomes import BusinessOutcomeEvent, OutcomeSource, OutcomeType
from clientplatform.domain.sales_followup import SalesFollowupStopReason
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.attribution_repository import AttributionRepository
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.infrastructure.outcome_repository import OutcomeRepository
from clientplatform.infrastructure.sales_followup_repository import SalesFollowupRepository
from services.db import get_db, get_db_ro


def create_booking_slot(
    *,
    actor: TenantContext,
    offering_id: str,
    local_start: str,
    duration_minutes: int,
) -> BookingSlotView:
    with get_db() as conn:
        return BookingRepository(conn).create_slot(
            actor=actor,
            offering_id=offering_id,
            local_start=local_start,
            duration_minutes=duration_minutes,
        )


def list_booking_slots(
    *,
    actor: TenantContext,
    offering_id: str | None = None,
    include_unavailable: bool = False,
) -> list[BookingSlotView]:
    with get_db_ro() as conn:
        return BookingRepository(conn).list_slots(
            actor=actor,
            offering_id=offering_id,
            include_unavailable=include_unavailable,
        )


def list_customer_businesses(*, telegram_user_id: int) -> list[CustomerBusinessLink]:
    with get_db_ro() as conn:
        member_businesses = active_member_business_ids(
            conn,
            telegram_user_id=telegram_user_id,
        )
        links = BookingRepository(conn).list_customer_businesses(
            telegram_user_id=telegram_user_id,
        )
        return [link for link in links if link.business_id not in member_businesses]


def list_customer_booking_slots(
    *,
    telegram_user_id: int,
    business_id: str,
) -> list[BookingSlotView]:
    with get_db_ro() as conn:
        assert_external_customer(
            conn,
            telegram_user_id=telegram_user_id,
            business_id=business_id,
        )
        return BookingRepository(conn).list_open_slots_for_customer(
            telegram_user_id=telegram_user_id,
            business_id=business_id,
        )


def get_customer_booking(
    *,
    telegram_user_id: int,
    business_id: str,
    slot_id: str,
    now: datetime | str | None = None,
) -> BookingClaim:
    """Return a booked customer appointment only while its start is still future.

    Scheduler reminders use this application boundary. Booking rows remain
    readable in the repository for historical/reporting use, but an overdue job
    after process downtime must not treat a meeting that already started as an
    active reminder target.
    """

    with get_db_ro() as conn:
        assert_external_customer(
            conn,
            telegram_user_id=telegram_user_id,
            business_id=business_id,
        )
        claim = BookingRepository(conn).get_customer_booking(
            telegram_user_id=telegram_user_id,
            business_id=business_id,
            slot_id=slot_id,
        )
    current = normalize_utc_datetime(
        str(now or datetime.now(timezone.utc).isoformat(timespec="seconds")),
        field_name="now",
    )
    if datetime.fromisoformat(claim.slot.slot.starts_at) <= datetime.fromisoformat(current):
        raise BookingNotFound("запись уже началась")
    return claim


def _finalize_customer_booking_claim(conn: Any, claim: BookingClaim) -> BookingClaim:
    booked_at = claim.slot.slot.booked_at
    if booked_at is None:
        raise RuntimeError("booked slot is missing booked_at")
    occurred_at = datetime.fromisoformat(
        normalize_utc_datetime(booked_at, field_name="booked_at")
    )
    OutcomeRepository(conn).append(
        BusinessOutcomeEvent(
            id=str(uuid4()),
            business_id=claim.slot.slot.business_id,
            outcome_type=OutcomeType.BOOKING_CREATED,
            occurred_at=occurred_at,
            source=OutcomeSource(
                source_type="booking_slot",
                source_id=claim.slot.slot.id,
            ),
            customer_id=claim.customer_id,
            subject_ref=f"booking_slot:{claim.slot.slot.id}",
            money=None,
            idempotency_key=f"booking_created:{claim.slot.slot.id}",
            metadata={},
            metadata_version=1,
            created_at=datetime.now(timezone.utc),
        )
    )
    # Any canonical booking path must inherit an already-established customer
    # acquisition first touch. If the customer has no attribution yet, this is
    # a no-op. The promoted-booking path intentionally repeats this link after
    # capturing its verified token so direct cpa_* booking remains atomic too.
    AttributionRepository(conn).link_booking_from_customer(
        business_id=claim.slot.slot.business_id,
        customer_id=claim.customer_id,
        booking_slot_id=claim.slot.slot.id,
    )
    SalesFollowupRepository(conn).stop_for_customer_conversion(
        business_id=claim.slot.slot.business_id,
        customer_id=claim.customer_id,
        reason=SalesFollowupStopReason.BOOKING,
        now=booked_at,
    )
    return claim


def book_customer_slot_in_transaction(
    conn: Any,
    *,
    telegram_user_id: int,
    business_id: str,
    slot_id: str,
) -> BookingClaim:
    """Canonical booking mutation for callers that already own the transaction."""

    assert_external_customer(
        conn,
        telegram_user_id=telegram_user_id,
        business_id=business_id,
    )
    claim = BookingRepository(conn).book_slot(
        telegram_user_id=telegram_user_id,
        business_id=business_id,
        slot_id=slot_id,
    )
    return _finalize_customer_booking_claim(conn, claim)


def book_customer_slot_by_customer_in_transaction(
    conn: Any,
    *,
    business_id: str,
    customer_id: str,
    slot_id: str,
) -> BookingClaim:
    """Canonical booking mutation for a server-resolved non-Telegram customer."""

    claim = BookingRepository(conn).book_slot_for_customer_id(
        business_id=business_id,
        customer_id=customer_id,
        slot_id=slot_id,
    )
    return _finalize_customer_booking_claim(conn, claim)


def book_customer_slot(
    *,
    telegram_user_id: int,
    business_id: str,
    slot_id: str,
) -> BookingClaim:
    """Book a slot and append its canonical outcome in the same transaction."""

    with get_db() as conn:
        return book_customer_slot_in_transaction(
            conn,
            telegram_user_id=telegram_user_id,
            business_id=business_id,
            slot_id=slot_id,
        )

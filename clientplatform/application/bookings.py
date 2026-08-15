from __future__ import annotations

from datetime import datetime, timezone

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
from clientplatform.domain.outcomes import (
    BusinessOutcomeEvent,
    OutcomeEventType,
    OutcomeSource,
    canonical_metadata_json,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.infrastructure.outcome_ledger import OutcomeLedger
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


def book_customer_slot(
    *,
    telegram_user_id: int,
    business_id: str,
    slot_id: str,
) -> BookingClaim:
    with get_db() as conn:
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
        slot = claim.slot.slot
        OutcomeLedger(conn).append(
            BusinessOutcomeEvent(
                business_id=slot.business_id,
                event_type=OutcomeEventType.BOOKING_CREATED,
                subject_type="booking_slot",
                subject_id=slot.id,
                occurred_at=slot.booked_at or slot.updated_at,
                idempotency_key=f"booking_created:{slot.id}",
                source=OutcomeSource.CLIENTPLATFORM,
                metadata_json=canonical_metadata_json(
                    {
                        "duration_minutes": slot.duration_minutes,
                        "ends_at": slot.ends_at,
                        "offering_id": slot.offering_id,
                        "starts_at": slot.starts_at,
                    }
                ),
            )
        )
        return claim

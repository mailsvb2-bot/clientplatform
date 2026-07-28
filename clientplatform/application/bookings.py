from __future__ import annotations

from clientplatform.domain.bookings import (
    BookingClaim,
    BookingSlotView,
    CustomerBusinessLink,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.booking_repository import BookingRepository
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
        return BookingRepository(conn).list_customer_businesses(
            telegram_user_id=telegram_user_id,
        )


def list_customer_booking_slots(
    *,
    telegram_user_id: int,
    business_id: str,
) -> list[BookingSlotView]:
    with get_db_ro() as conn:
        return BookingRepository(conn).list_open_slots_for_customer(
            telegram_user_id=telegram_user_id,
            business_id=business_id,
        )


def book_customer_slot(
    *,
    telegram_user_id: int,
    business_id: str,
    slot_id: str,
) -> BookingClaim:
    with get_db() as conn:
        return BookingRepository(conn).book_slot(
            telegram_user_id=telegram_user_id,
            business_id=business_id,
            slot_id=slot_id,
        )

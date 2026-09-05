from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.activity import ActivityInvariantViolation, OfferingStatus
from clientplatform.domain.bookings import (
    BookingClaim,
    BookingInvariantViolation,
    BookingNotFound,
    BookingSlot,
    BookingSlotStatus,
    BookingSlotView,
    CustomerBusinessLink,
    booking_end,
    normalize_duration_minutes,
    normalize_telegram_principal,
    normalize_utc_datetime,
    parse_local_booking_start,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.core import PostgresCompatConnection


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _booking_lock_key(*parts: str) -> int:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    digest = hashlib.blake2b(
        payload,
        digest_size=8,
        person=b"cp-booking-v1",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _serialize_booking_write(
    conn: Any,
    *,
    namespace: str,
    business_id: str,
    subject_id: str,
) -> None:
    """Serialize one overlap invariant for the lifetime of the transaction.

    PostgreSQL gets a transaction-scoped advisory lock, so the lock is released
    automatically on commit or rollback. SQLite has no advisory locks; its local
    development fallback obtains the write reservation before the overlap check.
    """

    if isinstance(conn, PostgresCompatConnection):
        lock_key = _booking_lock_key(namespace, business_id, subject_id)
        conn.execute("SELECT pg_advisory_xact_lock(?)", (lock_key,)).fetchone()
        return
    if isinstance(conn, sqlite3.Connection) and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _slot_from_row(row: Any) -> BookingSlot:
    booked_customer_id = _value(row, "booked_customer_id", 7)
    booked_at = _value(row, "booked_at", 11)
    cancelled_at = _value(row, "cancelled_at", 12)
    return BookingSlot(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        offering_id=str(_value(row, "offering_id", 2)),
        starts_at=str(_value(row, "starts_at", 3)),
        ends_at=str(_value(row, "ends_at", 4)),
        duration_minutes=int(_value(row, "duration_minutes", 5)),
        status=BookingSlotStatus(str(_value(row, "status", 6))),
        booked_customer_id=None if booked_customer_id is None else str(booked_customer_id),
        created_by_member_id=str(_value(row, "created_by_member_id", 8)),
        created_at=str(_value(row, "created_at", 9)),
        updated_at=str(_value(row, "updated_at", 10)),
        booked_at=None if booked_at is None else str(booked_at),
        cancelled_at=None if cancelled_at is None else str(cancelled_at),
    )


def _view_from_row(row: Any) -> BookingSlotView:
    return BookingSlotView(
        slot=_slot_from_row(row),
        offering_title=str(_value(row, "offering_title", 13)),
        business_name=str(_value(row, "business_name", 14)),
        timezone=str(_value(row, "timezone", 15)),
    )


_SLOT_SELECT = """
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
"""


class BookingRepository:
    """Tenant-safe availability and customer self-booking repository."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)
        self._activity = ActivityRepository(conn)

    def _current_actor(self, actor: TenantContext) -> TenantContext:
        return self._tenancy.resolve_context(user_id=actor.user_id, business_id=actor.business_id)

    def create_slot(
        self,
        *,
        actor: TenantContext,
        offering_id: str,
        local_start: str,
        duration_minutes: int,
        now: str | None = None,
    ) -> BookingSlotView:
        current = self._current_actor(actor)
        current.assert_can_manage_programs()
        offering = self._activity.get_offering(actor=current, offering_id=offering_id)
        if offering.status != OfferingStatus.ACTIVE:
            raise ActivityInvariantViolation("archived offering cannot receive booking slots")
        profile = self._activity.get_profile(actor=current)
        timestamp = normalize_utc_datetime(str(now or _utc_now()), field_name="now")
        starts_at = parse_local_booking_start(
            local_start,
            timezone_name=profile.timezone,
            now_utc=timestamp,
        )
        duration = normalize_duration_minutes(duration_minutes)
        ends_at = booking_end(starts_at, duration)
        if datetime.fromisoformat(starts_at) <= datetime.fromisoformat(timestamp):
            raise BookingInvariantViolation("время записи должно быть в будущем")
        _serialize_booking_write(
            self._conn,
            namespace="offering-slot-overlap",
            business_id=current.business_id,
            subject_id=offering.id,
        )
        overlap = self._conn.execute(
            """
            SELECT id FROM booking_slots
            WHERE business_id=? AND offering_id=?
              AND status IN ('open', 'booked')
              AND starts_at < ? AND ends_at > ?
            LIMIT 1
            """,
            (current.business_id, offering.id, ends_at, starts_at),
        ).fetchone()
        if overlap is not None:
            raise BookingInvariantViolation("для этого предложения время пересекается с другим слотом")
        slot_id = str(uuid4())
        self._conn.execute(
            """
            INSERT INTO booking_slots(
                id, business_id, offering_id, starts_at, ends_at,
                duration_minutes, status, booked_customer_id,
                created_by_member_id, created_at, updated_at,
                booked_at, cancelled_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'open', NULL, ?, ?, ?, NULL, NULL)
            """,
            (
                slot_id,
                current.business_id,
                offering.id,
                starts_at,
                ends_at,
                duration,
                current.membership_id,
                timestamp,
                timestamp,
            ),
        )
        return self.get_slot(actor=current, slot_id=slot_id)

    def get_slot(self, *, actor: TenantContext, slot_id: str) -> BookingSlotView:
        current = self._current_actor(actor)
        normalized_id = normalize_uuid(slot_id, field_name="booking_slot_id")
        row = self._conn.execute(
            _SLOT_SELECT + " WHERE bs.id=? AND bs.business_id=? LIMIT 1",
            (normalized_id, current.business_id),
        ).fetchone()
        if row is None:
            raise BookingNotFound("booking slot was not found")
        return _view_from_row(row)

    def list_slots(
        self,
        *,
        actor: TenantContext,
        offering_id: str | None = None,
        include_unavailable: bool = False,
        now: str | None = None,
    ) -> list[BookingSlotView]:
        current = self._current_actor(actor)
        current.assert_can_view_customer_records()
        timestamp = normalize_utc_datetime(str(now or _utc_now()), field_name="now")
        params: list[Any] = [current.business_id, timestamp]
        where = " WHERE bs.business_id=? AND bs.starts_at>=?"
        if offering_id is not None:
            normalized_offering = normalize_uuid(offering_id, field_name="offering_id")
            self._activity.get_offering(actor=current, offering_id=normalized_offering)
            where += " AND bs.offering_id=?"
            params.append(normalized_offering)
        if not include_unavailable:
            where += " AND bs.status='open' AND bo.status='active'"
        rows = self._conn.execute(
            _SLOT_SELECT + where + " ORDER BY bs.starts_at, bs.id",
            tuple(params),
        ).fetchall()
        return [_view_from_row(row) for row in rows]

    def list_customer_businesses(self, *, telegram_user_id: int) -> list[CustomerBusinessLink]:
        principal = normalize_telegram_principal(telegram_user_id)
        rows = self._conn.execute(
            """
            SELECT ci.business_id, b.name AS business_name, ci.customer_id
            FROM customer_identities ci
            JOIN customers c
              ON c.id=ci.customer_id AND c.business_id=ci.business_id AND c.status='active'
            JOIN businesses b
              ON b.id=ci.business_id AND b.status='active'
            WHERE ci.platform='telegram' AND ci.external_subject=? AND ci.status='active'
            ORDER BY b.name, ci.business_id
            """,
            (str(principal),),
        ).fetchall()
        return [
            CustomerBusinessLink(
                business_id=str(_value(row, "business_id", 0)),
                business_name=str(_value(row, "business_name", 1)),
                customer_id=str(_value(row, "customer_id", 2)),
            )
            for row in rows
        ]

    def _customer_link(self, *, telegram_user_id: int, business_id: str) -> CustomerBusinessLink:
        normalized_business = normalize_uuid(business_id, field_name="business_id")
        matches = [
            item
            for item in self.list_customer_businesses(telegram_user_id=telegram_user_id)
            if item.business_id == normalized_business
        ]
        if not matches:
            raise BookingNotFound("Вы не подключены к этому бизнесу")
        return matches[0]

    def _customer_link_by_customer(
        self,
        *,
        business_id: str,
        customer_id: str,
    ) -> CustomerBusinessLink:
        business = normalize_uuid(business_id, field_name="business_id")
        customer = normalize_uuid(customer_id, field_name="customer_id")
        row = self._conn.execute(
            """
            SELECT c.business_id, b.name AS business_name, c.id AS customer_id
            FROM customers c
            JOIN businesses b ON b.id=c.business_id AND b.status='active'
            WHERE c.business_id=? AND c.id=? AND c.status='active'
            LIMIT 1
            """,
            (business, customer),
        ).fetchone()
        if row is None:
            raise BookingNotFound("Вы не подключены к этому бизнесу")
        return CustomerBusinessLink(
            business_id=str(_value(row, "business_id", 0)),
            business_name=str(_value(row, "business_name", 1)),
            customer_id=str(_value(row, "customer_id", 2)),
        )

    def get_customer_booking(
        self,
        *,
        telegram_user_id: int,
        business_id: str,
        slot_id: str,
    ) -> BookingClaim:
        link = self._customer_link(
            telegram_user_id=telegram_user_id,
            business_id=business_id,
        )
        return self.get_customer_booking_by_customer(
            business_id=link.business_id,
            customer_id=link.customer_id,
            slot_id=slot_id,
        )

    def get_customer_booking_by_customer(
        self,
        *,
        business_id: str,
        customer_id: str,
        slot_id: str,
    ) -> BookingClaim:
        link = self._customer_link_by_customer(
            business_id=business_id, customer_id=customer_id
        )
        normalized_slot = normalize_uuid(slot_id, field_name="booking_slot_id")
        row = self._conn.execute(
            _SLOT_SELECT
            + " WHERE bs.id=? AND bs.business_id=? AND bs.booked_customer_id=? LIMIT 1",
            (normalized_slot, link.business_id, link.customer_id),
        ).fetchone()
        if row is None:
            raise BookingNotFound("запись не найдена")
        view = _view_from_row(row)
        if view.slot.status != BookingSlotStatus.BOOKED:
            raise BookingNotFound("запись больше не активна")
        return BookingClaim(slot=view, customer_id=link.customer_id)

    def list_open_slots_for_customer(
        self,
        *,
        telegram_user_id: int,
        business_id: str,
        now: str | None = None,
    ) -> list[BookingSlotView]:
        link = self._customer_link(telegram_user_id=telegram_user_id, business_id=business_id)
        return self.list_open_slots_for_customer_id(
            business_id=link.business_id, customer_id=link.customer_id, now=now
        )

    def list_open_slots_for_customer_id(
        self,
        *,
        business_id: str,
        customer_id: str,
        now: str | None = None,
    ) -> list[BookingSlotView]:
        link = self._customer_link_by_customer(
            business_id=business_id, customer_id=customer_id
        )
        timestamp = normalize_utc_datetime(str(now or _utc_now()), field_name="now")
        rows = self._conn.execute(
            _SLOT_SELECT
            + """
              WHERE bs.business_id=? AND bs.status='open' AND bs.starts_at>?
                AND bo.status='active'
              ORDER BY bs.starts_at, bs.id
            """,
            (link.business_id, timestamp),
        ).fetchall()
        return [_view_from_row(row) for row in rows]

    def book_slot(
        self,
        *,
        telegram_user_id: int,
        business_id: str,
        slot_id: str,
        now: str | None = None,
    ) -> BookingClaim:
        link = self._customer_link(telegram_user_id=telegram_user_id, business_id=business_id)
        return self.book_slot_for_customer_id(
            business_id=link.business_id,
            customer_id=link.customer_id,
            slot_id=slot_id,
            now=now,
        )

    def book_slot_for_customer_id(
        self,
        *,
        business_id: str,
        customer_id: str,
        slot_id: str,
        now: str | None = None,
    ) -> BookingClaim:
        link = self._customer_link_by_customer(
            business_id=business_id, customer_id=customer_id
        )
        normalized_slot = normalize_uuid(slot_id, field_name="booking_slot_id")
        timestamp = normalize_utc_datetime(str(now or _utc_now()), field_name="now")
        _serialize_booking_write(
            self._conn,
            namespace="customer-booking-overlap",
            business_id=link.business_id,
            subject_id=link.customer_id,
        )
        row = self._conn.execute(
            _SLOT_SELECT
            + " WHERE bs.id=? AND bs.business_id=? AND bo.status='active' LIMIT 1",
            (normalized_slot, link.business_id),
        ).fetchone()
        if row is None:
            raise BookingNotFound("время записи не найдено")
        view = _view_from_row(row)
        if view.slot.status == BookingSlotStatus.BOOKED:
            if view.slot.booked_customer_id == link.customer_id:
                return BookingClaim(slot=view, customer_id=link.customer_id)
            raise BookingInvariantViolation("это время уже занято")
        if view.slot.status != BookingSlotStatus.OPEN:
            raise BookingInvariantViolation("это время больше недоступно")
        if datetime.fromisoformat(view.slot.starts_at) <= datetime.fromisoformat(timestamp):
            raise BookingInvariantViolation("это время уже прошло")
        conflict = self._conn.execute(
            """
            SELECT id FROM booking_slots
            WHERE business_id=? AND booked_customer_id=? AND status='booked'
              AND starts_at < ? AND ends_at > ?
            LIMIT 1
            """,
            (
                link.business_id,
                link.customer_id,
                view.slot.ends_at,
                view.slot.starts_at,
            ),
        ).fetchone()
        if conflict is not None:
            raise BookingInvariantViolation("у Вас уже есть пересекающаяся запись")
        cursor = self._conn.execute(
            """
            UPDATE booking_slots
            SET status='booked', booked_customer_id=?, booked_at=?, updated_at=?
            WHERE id=? AND business_id=? AND status='open' AND booked_customer_id IS NULL
              AND EXISTS (
                  SELECT 1 FROM business_offerings bo
                  WHERE bo.id=booking_slots.offering_id
                    AND bo.business_id=booking_slots.business_id
                    AND bo.status='active'
              )
            """,
            (
                link.customer_id,
                timestamp,
                timestamp,
                normalized_slot,
                link.business_id,
            ),
        )
        if getattr(cursor, "rowcount", 1) == 0:
            raise BookingInvariantViolation("это время только что занял другой клиент")
        booked = self._conn.execute(
            _SLOT_SELECT + " WHERE bs.id=? AND bs.business_id=? LIMIT 1",
            (normalized_slot, link.business_id),
        ).fetchone()
        if booked is None:
            raise BookingNotFound("забронированное время не найдено")
        return BookingClaim(slot=_view_from_row(booked), customer_id=link.customer_id)

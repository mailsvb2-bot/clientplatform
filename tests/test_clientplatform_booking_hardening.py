from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from clientplatform.domain.bookings import BookingInvariantViolation, parse_local_booking_start
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.booking_repository import BookingRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_bookings,
    clientplatform_customers,
    clientplatform_tenancy,
)


@contextmanager
def _managed_connection(path: Path):
    conn = _connection(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


class ClientPlatformBookingHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "booking.db"
        self.conn = _connection(self.path)
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_bookings.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        activity = ActivityRepository(self.conn)
        business = tenancy.create_business(owner_user_id=101, name="Практика Марии")
        self.owner = tenancy.resolve_context(user_id=101, business_id=business.business.id)
        activity.upsert_profile(
            actor=self.owner,
            activity_description="Консультирую родителей",
            timezone_name="Europe/Amsterdam",
            now="2026-07-28T12:00:00+00:00",
        )
        consultations = activity.enable_capability(
            actor=self.owner,
            connector_key="consultations",
            now="2026-07-28T12:00:00+00:00",
        )
        services = activity.enable_capability(
            actor=self.owner,
            connector_key="services",
            now="2026-07-28T12:00:00+00:00",
        )
        self.first_offering = activity.create_offering(
            actor=self.owner,
            capability_id=consultations.id,
            title="Первая консультация",
            description="60 минут",
            now="2026-07-28T12:00:00+00:00",
        )
        self.second_offering = activity.create_offering(
            actor=self.owner,
            capability_id=services.id,
            title="Диагностика",
            description="60 минут",
            now="2026-07-28T12:00:00+00:00",
        )
        issued = activity.issue_customer_invite(
            actor=self.owner,
            now="2026-07-28T12:00:00+00:00",
        )
        claim = activity.claim_customer_invite(
            token=issued.token,
            telegram_user_id=700001,
            username="customer",
            display_name="Клиент",
            now="2026-07-28T12:05:00+00:00",
        )
        self.customer_id = claim.customer_id
        self.business_id = business.business.id
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def test_dst_gap_and_repeated_local_time_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "не существует"):
            parse_local_booking_start(
                "29.03.2026 02:30",
                timezone_name="Europe/Amsterdam",
            )
        with self.assertRaisesRegex(ValueError, "повторяется"):
            parse_local_booking_start(
                "25.10.2026 02:30",
                timezone_name="Europe/Amsterdam",
            )

    def test_booking_start_accepts_short_year_and_yearless_future_date(self) -> None:
        short_year = parse_local_booking_start(
            "10.08.26 15:00",
            timezone_name="Europe/Amsterdam",
            now_utc="2026-08-07T12:00:00+00:00",
        )
        yearless = parse_local_booking_start(
            "10.08 15:00",
            timezone_name="Europe/Amsterdam",
            now_utc="2026-08-07T12:00:00+00:00",
        )
        self.assertEqual(short_year, "2026-08-10T13:00:00+00:00")
        self.assertEqual(yearless, short_year)

    def test_yearless_booking_date_rolls_to_next_year_after_date_passes(self) -> None:
        parsed = parse_local_booking_start(
            "10.08 15:00",
            timezone_name="Europe/Amsterdam",
            now_utc="2026-08-11T12:00:00+00:00",
        )
        self.assertEqual(parsed, "2027-08-10T13:00:00+00:00")

    def test_yearless_leap_day_finds_next_real_calendar_occurrence(self) -> None:
        parsed = parse_local_booking_start(
            "29.02 15:00",
            timezone_name="Europe/Amsterdam",
            now_utc="2026-08-07T12:00:00+00:00",
        )
        self.assertEqual(parsed, "2028-02-29T14:00:00+00:00")

    def test_invalid_yearless_calendar_date_has_human_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "такой даты"):
            parse_local_booking_start(
                "31.04 15:00",
                timezone_name="Europe/Amsterdam",
                now_utc="2026-08-07T12:00:00+00:00",
            )

    def test_bad_booking_date_explains_all_supported_human_formats(self) -> None:
        with self.assertRaisesRegex(ValueError, "10.08 15:00"):
            parse_local_booking_start(
                "завтра после обеда",
                timezone_name="Europe/Amsterdam",
                now_utc="2026-08-07T12:00:00+00:00",
            )

    def test_sqlite_serializes_overlapping_slot_publication(self) -> None:
        gate = threading.Barrier(2)

        def publish(local_start: str) -> str:
            with _managed_connection(self.path) as conn:
                gate.wait(timeout=10)
                try:
                    BookingRepository(conn).create_slot(
                        actor=self.owner,
                        offering_id=self.first_offering.id,
                        local_start=local_start,
                        duration_minutes=60,
                        now="2026-07-28T12:00:00+00:00",
                    )
                except BookingInvariantViolation:
                    return "conflict"
                return "created"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(publish, ("02.08.2026 10:00", "02.08.2026 10:30")))
        self.assertCountEqual(results, ["created", "conflict"])
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM booking_slots WHERE offering_id=?",
            (self.first_offering.id,),
        ).fetchone()
        self.assertEqual(int(row["c"]), 1)

    def test_sqlite_serializes_overlapping_bookings_for_one_customer(self) -> None:
        bookings = BookingRepository(self.conn)
        first = bookings.create_slot(
            actor=self.owner,
            offering_id=self.first_offering.id,
            local_start="03.08.2026 10:00",
            duration_minutes=60,
            now="2026-07-28T12:00:00+00:00",
        )
        second = bookings.create_slot(
            actor=self.owner,
            offering_id=self.second_offering.id,
            local_start="03.08.2026 10:30",
            duration_minutes=60,
            now="2026-07-28T12:00:00+00:00",
        )
        self.conn.commit()
        gate = threading.Barrier(2)

        def book(slot_id: str) -> str:
            with _managed_connection(self.path) as conn:
                gate.wait(timeout=10)
                try:
                    BookingRepository(conn).book_slot(
                        telegram_user_id=700001,
                        business_id=self.business_id,
                        slot_id=slot_id,
                        now="2026-07-28T12:10:00+00:00",
                    )
                except BookingInvariantViolation:
                    return "conflict"
                return "booked"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(book, (first.slot.id, second.slot.id)))
        self.assertCountEqual(results, ["booked", "conflict"])
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS c FROM booking_slots
            WHERE business_id=? AND booked_customer_id=? AND status='booked'
            """,
            (self.business_id, self.customer_id),
        ).fetchone()
        self.assertEqual(int(row["c"]), 1)

    def test_open_slot_with_existing_customer_fails_closed(self) -> None:
        bookings = BookingRepository(self.conn)
        slot = bookings.create_slot(
            actor=self.owner,
            offering_id=self.first_offering.id,
            local_start="04.08.2026 10:00",
            duration_minutes=60,
            now="2026-07-28T12:00:00+00:00",
        )
        self.conn.execute(
            "UPDATE booking_slots SET booked_customer_id=? WHERE id=? AND business_id=?",
            (self.customer_id, slot.slot.id, self.business_id),
        )
        self.conn.commit()

        with self.assertRaisesRegex(BookingInvariantViolation, "только что занял"):
            bookings.book_slot_for_customer_id(
                business_id=self.business_id,
                customer_id=self.customer_id,
                slot_id=slot.slot.id,
                now="2026-07-28T12:10:00+00:00",
            )

        row = self.conn.execute(
            "SELECT status, booked_at FROM booking_slots WHERE id=? AND business_id=?",
            (slot.slot.id, self.business_id),
        ).fetchone()
        self.assertEqual(str(row["status"]), "open")
        self.assertIsNone(row["booked_at"])


if __name__ == "__main__":
    unittest.main()

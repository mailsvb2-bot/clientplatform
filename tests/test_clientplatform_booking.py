from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from clientplatform.domain.bookings import (
    BookingInvariantViolation,
    BookingNotFound,
    BookingSlotStatus,
    format_booking_local,
    parse_local_booking_start,
)
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.privacy_manifest import validate_clientplatform_privacy_manifest
from services.db.schema import (
    clientplatform_activity,
    clientplatform_bookings,
    clientplatform_customers,
    clientplatform_tenancy,
)


class ClientPlatformBookingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_bookings.ensure(self.conn)
        self.tenancy = TenancyRepository(self.conn)
        self.activity = ActivityRepository(self.conn)
        self.bookings = BookingRepository(self.conn)
        self.business = self.tenancy.create_business(owner_user_id=101, name="Практика Марии")
        self.owner = self.tenancy.resolve_context(
            user_id=101,
            business_id=self.business.business.id,
        )
        self.activity.upsert_profile(
            actor=self.owner,
            activity_description="Консультирую родителей",
            timezone_name="Europe/Amsterdam",
            now="2026-07-28T12:00:00+00:00",
        )
        capability = self.activity.enable_capability(
            actor=self.owner,
            connector_key="consultations",
            now="2026-07-28T12:00:00+00:00",
        )
        self.offering = self.activity.create_offering(
            actor=self.owner,
            capability_id=capability.id,
            title="Первая консультация",
            description="60 минут и письменный план",
            now="2026-07-28T12:00:00+00:00",
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _connect_customer(self, *, telegram_user_id: int, name: str) -> str:
        issued = self.activity.issue_customer_invite(
            actor=self.owner,
            now="2026-07-28T12:00:00+00:00",
        )
        claim = self.activity.claim_customer_invite(
            token=issued.token,
            telegram_user_id=telegram_user_id,
            username=f"user_{telegram_user_id}",
            display_name=name,
            now="2026-07-28T12:05:00+00:00",
        )
        return claim.customer_id

    def test_local_datetime_round_trip_respects_business_timezone(self) -> None:
        utc_value = parse_local_booking_start(
            "31.07.2026 15:00",
            timezone_name="Europe/Amsterdam",
        )
        self.assertEqual(utc_value, "2026-07-31T13:00:00+00:00")
        self.assertEqual(
            format_booking_local(utc_value, timezone_name="Europe/Amsterdam"),
            "31.07.2026 15:00",
        )

    def test_owner_publishes_slot_and_connected_customer_books_it(self) -> None:
        slot = self.bookings.create_slot(
            actor=self.owner,
            offering_id=self.offering.id,
            local_start="31.07.2026 15:00",
            duration_minutes=60,
            now="2026-07-28T12:00:00+00:00",
        )
        self.assertEqual(slot.slot.status, BookingSlotStatus.OPEN)
        self.assertEqual(slot.local_start, "31.07.2026 15:00")
        self.assertEqual(slot.offering_title, "Первая консультация")

        customer_id = self._connect_customer(telegram_user_id=700001, name="Первый клиент")
        links = self.bookings.list_customer_businesses(telegram_user_id=700001)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].customer_id, customer_id)

        available = self.bookings.list_open_slots_for_customer(
            telegram_user_id=700001,
            business_id=self.business.business.id,
            now="2026-07-28T12:10:00+00:00",
        )
        self.assertEqual([item.slot.id for item in available], [slot.slot.id])

        claim = self.bookings.book_slot(
            telegram_user_id=700001,
            business_id=self.business.business.id,
            slot_id=slot.slot.id,
            now="2026-07-28T12:11:00+00:00",
        )
        self.assertEqual(claim.customer_id, customer_id)
        self.assertEqual(claim.slot.slot.status, BookingSlotStatus.BOOKED)
        self.assertEqual(claim.slot.slot.booked_customer_id, customer_id)
        self.assertEqual(
            self.bookings.list_open_slots_for_customer(
                telegram_user_id=700001,
                business_id=self.business.business.id,
                now="2026-07-28T12:12:00+00:00",
            ),
            [],
        )

        repeated = self.bookings.book_slot(
            telegram_user_id=700001,
            business_id=self.business.business.id,
            slot_id=slot.slot.id,
            now="2026-07-28T12:13:00+00:00",
        )
        self.assertEqual(repeated.customer_id, customer_id)

        restored = self.bookings.get_customer_booking(
            telegram_user_id=700001,
            business_id=self.business.business.id,
            slot_id=slot.slot.id,
        )
        self.assertEqual(restored.customer_id, customer_id)
        with self.assertRaises(BookingNotFound):
            self.bookings.get_customer_booking(
                telegram_user_id=700002,
                business_id=self.business.business.id,
                slot_id=slot.slot.id,
            )

    def test_slot_claim_is_atomic_between_two_customers(self) -> None:
        slot = self.bookings.create_slot(
            actor=self.owner,
            offering_id=self.offering.id,
            local_start="01.08.2026 10:00",
            duration_minutes=45,
            now="2026-07-28T12:00:00+00:00",
        )
        self._connect_customer(telegram_user_id=700001, name="Первый")
        self._connect_customer(telegram_user_id=700002, name="Второй")
        self.bookings.book_slot(
            telegram_user_id=700001,
            business_id=self.business.business.id,
            slot_id=slot.slot.id,
            now="2026-07-28T12:10:00+00:00",
        )
        with self.assertRaisesRegex(BookingInvariantViolation, "уже занято"):
            self.bookings.book_slot(
                telegram_user_id=700002,
                business_id=self.business.business.id,
                slot_id=slot.slot.id,
                now="2026-07-28T12:11:00+00:00",
            )

    def test_overlapping_offering_slots_and_customer_bookings_are_rejected(self) -> None:
        first = self.bookings.create_slot(
            actor=self.owner,
            offering_id=self.offering.id,
            local_start="02.08.2026 10:00",
            duration_minutes=60,
            now="2026-07-28T12:00:00+00:00",
        )
        with self.assertRaisesRegex(BookingInvariantViolation, "пересекается"):
            self.bookings.create_slot(
                actor=self.owner,
                offering_id=self.offering.id,
                local_start="02.08.2026 10:30",
                duration_minutes=60,
                now="2026-07-28T12:00:00+00:00",
            )

        second_capability = self.activity.enable_capability(
            actor=self.owner,
            connector_key="services",
            now="2026-07-28T12:00:00+00:00",
        )
        second_offering = self.activity.create_offering(
            actor=self.owner,
            capability_id=second_capability.id,
            title="Диагностика",
            description="Проверка и рекомендации",
            now="2026-07-28T12:00:00+00:00",
        )
        second = self.bookings.create_slot(
            actor=self.owner,
            offering_id=second_offering.id,
            local_start="02.08.2026 10:30",
            duration_minutes=60,
            now="2026-07-28T12:00:00+00:00",
        )
        self._connect_customer(telegram_user_id=700001, name="Клиент")
        self.bookings.book_slot(
            telegram_user_id=700001,
            business_id=self.business.business.id,
            slot_id=first.slot.id,
            now="2026-07-28T12:10:00+00:00",
        )
        with self.assertRaisesRegex(BookingInvariantViolation, "пересекающаяся"):
            self.bookings.book_slot(
                telegram_user_id=700001,
                business_id=self.business.business.id,
                slot_id=second.slot.id,
                now="2026-07-28T12:11:00+00:00",
            )

    def test_unknown_customer_and_cross_tenant_slot_are_invisible(self) -> None:
        slot = self.bookings.create_slot(
            actor=self.owner,
            offering_id=self.offering.id,
            local_start="03.08.2026 12:00",
            duration_minutes=30,
            now="2026-07-28T12:00:00+00:00",
        )
        with self.assertRaises(BookingNotFound):
            self.bookings.list_open_slots_for_customer(
                telegram_user_id=999999,
                business_id=self.business.business.id,
                now="2026-07-28T12:00:00+00:00",
            )

        other = self.tenancy.create_business(owner_user_id=202, name="Другой бизнес")
        other_actor = self.tenancy.resolve_context(user_id=202, business_id=other.business.id)
        with self.assertRaises(BookingNotFound):
            self.bookings.get_slot(actor=other_actor, slot_id=slot.slot.id)

    def test_past_time_and_invalid_duration_are_rejected(self) -> None:
        with self.assertRaisesRegex(BookingInvariantViolation, "в будущем"):
            self.bookings.create_slot(
                actor=self.owner,
                offering_id=self.offering.id,
                local_start="27.07.2026 10:00",
                duration_minutes=60,
                now="2026-07-28T12:00:00+00:00",
            )
        with self.assertRaises(ValueError):
            self.bookings.create_slot(
                actor=self.owner,
                offering_id=self.offering.id,
                local_start="31.07.2026 10:00",
                duration_minutes=5,
                now="2026-07-28T12:00:00+00:00",
            )

    def test_privacy_manifest_covers_booking_slots(self) -> None:
        report = validate_clientplatform_privacy_manifest(self.conn, require_complete=False)
        self.assertTrue(report.ok)
        self.assertIn("booking_slots", report.discovered_business_tables)


class ClientPlatformControlDefaultTests(unittest.TestCase):
    def test_default_is_enabled_and_explicit_zero_is_emergency_opt_out(self) -> None:
        from clientplatform.runtime.control_bot import control_bot_enabled

        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(control_bot_enabled())
        with patch.dict("os.environ", {"CLIENTPLATFORM_CONTROL_BOT_ENABLED": "0"}, clear=True):
            self.assertFalse(control_bot_enabled())
        with patch.dict("os.environ", {"CLIENTPLATFORM_CONTROL_BOT_ENABLED": "unexpected"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "enabled_invalid"):
                control_bot_enabled()


if __name__ == "__main__":
    unittest.main()

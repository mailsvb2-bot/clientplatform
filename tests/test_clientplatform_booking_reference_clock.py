from __future__ import annotations

import sqlite3
import unittest

from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.booking_repository import BookingRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_bookings,
    clientplatform_customers,
    clientplatform_tenancy,
)


class ClientPlatformBookingReferenceClockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_bookings.ensure(self.conn)

        tenancy = TenancyRepository(self.conn)
        activity = ActivityRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Практика")
        self.owner = tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        activity.upsert_profile(
            actor=self.owner,
            activity_description="Консультации",
            timezone_name="Europe/Amsterdam",
            now="2030-08-11T12:00:00+00:00",
        )
        capability = activity.enable_capability(
            actor=self.owner,
            connector_key="consultations",
            now="2030-08-11T12:00:00+00:00",
        )
        self.offering = activity.create_offering(
            actor=self.owner,
            capability_id=capability.id,
            title="Консультация",
            description="60 минут",
            now="2030-08-11T12:00:00+00:00",
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_yearless_slot_uses_repository_now_for_year_rollover(self) -> None:
        slot = BookingRepository(self.conn).create_slot(
            actor=self.owner,
            offering_id=self.offering.id,
            local_start="10.08 15:00",
            duration_minutes=60,
            now="2030-08-11T12:00:00+00:00",
        )

        self.assertEqual(slot.slot.starts_at, "2031-08-10T13:00:00+00:00")
        self.assertEqual(slot.slot.created_at, "2030-08-11T12:00:00+00:00")


if __name__ == "__main__":
    unittest.main()

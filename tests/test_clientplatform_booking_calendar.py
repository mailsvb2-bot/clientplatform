from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application.booking_reminders import schedule_booking_reminders
from clientplatform.domain.booking_calendar import booking_calendar_ics, google_calendar_url
from clientplatform.domain.bookings import BookingClaim, BookingSlot, BookingSlotStatus, BookingSlotView


def _claim() -> BookingClaim:
    business_id = str(uuid4())
    customer_id = str(uuid4())
    slot = BookingSlot(
        id=str(uuid4()),
        business_id=business_id,
        offering_id=str(uuid4()),
        starts_at="2026-08-10T13:00:00+00:00",
        ends_at="2026-08-10T14:00:00+00:00",
        duration_minutes=60,
        status=BookingSlotStatus.BOOKED,
        booked_customer_id=customer_id,
        created_by_member_id=str(uuid4()),
        created_at="2026-08-01T10:00:00+00:00",
        updated_at="2026-08-01T10:00:00+00:00",
        booked_at="2026-08-01T10:00:00+00:00",
    )
    return BookingClaim(
        slot=BookingSlotView(
            slot=slot,
            offering_title="Консультация",
            business_name="Практика",
            timezone="Europe/Amsterdam",
        ),
        customer_id=customer_id,
    )


class ClientPlatformBookingCalendarTests(unittest.TestCase):
    def test_ics_contains_event_and_two_native_alarms(self) -> None:
        content = booking_calendar_ics(_claim().slot).decode("utf-8")
        self.assertIn("BEGIN:VEVENT", content)
        self.assertIn("DTSTART:20260810T130000Z", content)
        self.assertIn("TRIGGER:-PT24H", content)
        self.assertIn("TRIGGER:-PT1H", content)
        self.assertEqual(content.count("BEGIN:VALARM"), 2)

    def test_google_calendar_url_contains_utc_range(self) -> None:
        url = google_calendar_url(_claim().slot)
        self.assertTrue(url.startswith("https://calendar.google.com/calendar/render?"))
        self.assertIn("20260810T130000Z%2F20260810T140000Z", url)

    def test_reminders_are_idempotently_scheduled_at_24h_and_1h(self) -> None:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def fake_add_job(*args: object, **kwargs: object) -> bool:
            calls.append((args, kwargs))
            return True

        with patch(
            "clientplatform.application.booking_reminders.add_job",
            side_effect=fake_add_job,
        ):
            scheduled = schedule_booking_reminders(
                telegram_user_id=700001,
                claim=_claim(),
                now=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(scheduled, (1440, 60))
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("clientplatform-booking:" in str(call[1]["job_key"]) for call in calls))


if __name__ == "__main__":
    unittest.main()

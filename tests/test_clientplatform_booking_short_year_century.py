from __future__ import annotations

import unittest

from clientplatform.domain.bookings import parse_local_booking_start


class ClientPlatformBookingShortYearCenturyTests(unittest.TestCase):
    def test_short_year_69_stays_in_current_century(self) -> None:
        parsed = parse_local_booking_start(
            "10.08.69 15:00",
            timezone_name="UTC",
            now_utc="2026-08-07T12:00:00+00:00",
        )
        self.assertEqual(parsed, "2069-08-10T15:00:00+00:00")

    def test_short_year_99_stays_in_current_century(self) -> None:
        parsed = parse_local_booking_start(
            "10.08.99 15:00",
            timezone_name="UTC",
            now_utc="2026-08-07T12:00:00+00:00",
        )
        self.assertEqual(parsed, "2099-08-10T15:00:00+00:00")

    def test_short_year_uses_reference_century_after_2100(self) -> None:
        parsed = parse_local_booking_start(
            "10.08.26 15:00",
            timezone_name="UTC",
            now_utc="2105-08-07T12:00:00+00:00",
        )
        self.assertEqual(parsed, "2126-08-10T15:00:00+00:00")


if __name__ == "__main__":
    unittest.main()

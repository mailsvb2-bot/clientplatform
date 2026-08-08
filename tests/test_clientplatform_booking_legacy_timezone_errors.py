from __future__ import annotations

import unittest

from clientplatform.domain.bookings import format_booking_local, parse_local_booking_start


class ClientPlatformBookingLegacyTimezoneErrorTests(unittest.TestCase):
    def test_pathlike_timezone_is_normalized_during_booking_parse(self) -> None:
        for timezone_name in ("/etc/passwd", "../Etc/UTC"):
            with self.subTest(timezone_name=timezone_name):
                with self.assertRaisesRegex(ValueError, "неизвестный часовой пояс бизнеса"):
                    parse_local_booking_start(
                        "10.08 15:00",
                        timezone_name=timezone_name,
                        now_utc="2026-08-08T12:00:00+00:00",
                    )

    def test_pathlike_timezone_is_normalized_during_local_formatting(self) -> None:
        for timezone_name in ("/etc/passwd", "../Etc/UTC"):
            with self.subTest(timezone_name=timezone_name):
                with self.assertRaisesRegex(ValueError, "неизвестный часовой пояс бизнеса"):
                    format_booking_local(
                        "2026-08-10T13:00:00+00:00",
                        timezone_name=timezone_name,
                    )


if __name__ == "__main__":
    unittest.main()

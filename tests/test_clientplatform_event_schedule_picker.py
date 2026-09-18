from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from clientplatform.presentation.event_schedule_picker import (
    QUICK_DURATIONS,
    QUICK_START_TIMES,
    calendar_days,
    local_today,
    month_key,
    parse_calendar_date,
    parse_month_key,
    parse_quick_duration,
    parse_quick_time,
    session_window_text,
    shift_month,
    validate_calendar_month,
    webinar_venue,
)


class WebinarVenuePickerTests(unittest.TestCase):
    def test_known_services_have_safe_https_launch_boundaries(self) -> None:
        self.assertEqual(webinar_venue("telemost").open_url, "https://telemost.yandex.ru/")
        self.assertTrue(str(webinar_venue("zoom").open_url).startswith("https://"))
        self.assertTrue(str(webinar_venue("webinar_ru").open_url).startswith("https://"))
        self.assertTrue(str(webinar_venue("getcourse").open_url).startswith("https://"))
        self.assertIsNone(webinar_venue("other").open_url)

    def test_ucr_is_explicitly_not_claimed_as_public_webinar_room(self) -> None:
        ucr = webinar_venue("ucr")
        self.assertFalse(ucr.public_room_supported)
        self.assertIsNone(ucr.open_url)
        self.assertIn("не выдаёт", ucr.note)

    def test_unknown_service_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            webinar_venue("invented-provider")


class WebinarCalendarPickerTests(unittest.TestCase):
    def test_local_today_uses_event_timezone(self) -> None:
        now = datetime(2026, 9, 17, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(local_today("Europe/Moscow", now=now), date(2026, 9, 18))
        self.assertEqual(local_today("Europe/Amsterdam", now=now), date(2026, 9, 18))
        with self.assertRaises(ValueError):
            local_today("Not/AZone", now=now)
        with self.assertRaises(ValueError):
            local_today("Europe/Moscow", now=datetime(2026, 9, 17, 22, 30))

    def test_month_keys_and_navigation_are_bounded(self) -> None:
        self.assertEqual(month_key(2026, 9), "202609")
        self.assertEqual(parse_month_key("202609"), (2026, 9))
        self.assertEqual(shift_month(2026, 12, 1), (2027, 1))
        self.assertEqual(shift_month(2027, 1, -1), (2026, 12))
        for bad in ("", "20261", "202613", "abc123"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_month_key(bad)
        with self.assertRaises(ValueError):
            shift_month(2026, 9, 2)

    def test_calendar_disables_past_days_and_keeps_future_selectable(self) -> None:
        minimum = date(2026, 9, 17)
        weeks = calendar_days(year=2026, month=9, minimum=minimum)
        by_day = {cell.day: cell for week in weeks for cell in week if cell.day}
        self.assertFalse(by_day[16].enabled)
        self.assertIsNone(by_day[16].value)
        self.assertTrue(by_day[17].enabled)
        self.assertEqual(by_day[17].value, "2026-09-17")
        self.assertTrue(by_day[30].enabled)

    def test_calendar_rejects_past_and_excessively_distant_months(self) -> None:
        minimum = date(2026, 9, 17)
        with self.assertRaises(ValueError):
            validate_calendar_month(year=2026, month=8, minimum=minimum)
        with self.assertRaises(ValueError):
            validate_calendar_month(year=2028, month=4, minimum=minimum)
        with self.assertRaises(ValueError):
            validate_calendar_month(year=2026, month=9, minimum=minimum, max_months=50)
        with self.assertRaises(ValueError):
            parse_calendar_date("2026-09-16", minimum=minimum)
        self.assertEqual(
            parse_calendar_date("2026-10-05", minimum=minimum),
            date(2026, 10, 5),
        )

    def test_quick_time_duration_and_window_remove_manual_end_time(self) -> None:
        self.assertEqual(parse_quick_time("19:00"), "19:00")
        self.assertEqual(parse_quick_duration("120"), 120)
        self.assertIn("19:00", QUICK_START_TIMES)
        self.assertIn(120, QUICK_DURATIONS)
        self.assertEqual(
            session_window_text(
                selected_date=date(2026, 9, 25),
                start_time="19:00",
                duration_minutes=120,
            ),
            "25.09.2026 19:00-21:00",
        )
        with self.assertRaises(ValueError):
            parse_quick_time("19:15")
        with self.assertRaises(ValueError):
            parse_quick_duration("75")
        with self.assertRaises(ValueError):
            session_window_text(
                selected_date=date(2026, 9, 25),
                start_time="23:00",
                duration_minutes=120,
            )


if __name__ == "__main__":
    unittest.main()

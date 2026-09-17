from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from clientplatform.application.event_wizard import (
    MAX_EVENT_SESSIONS,
    MOSCOW_TIMEZONE,
    EventWizardSession,
    normalize_event_timezone,
    normalize_session_join_url,
    parse_session_count,
    parse_session_window,
    validate_session_sequence,
)


class EventWizardParsingTests(unittest.TestCase):
    def test_session_count_accepts_arbitrary_supported_length(self) -> None:
        self.assertEqual(parse_session_count("1"), 1)
        self.assertEqual(parse_session_count("два дня"), 2)
        self.assertEqual(parse_session_count("5 дней"), 5)
        self.assertEqual(parse_session_count(str(MAX_EVENT_SESSIONS)), MAX_EVENT_SESSIONS)
        for value in ("0", str(MAX_EVENT_SESSIONS + 1), "много"):
            with self.assertRaises(ValueError):
                parse_session_count(value)

    def test_timezone_requires_moscow_alias_or_real_iana_name(self) -> None:
        self.assertEqual(normalize_event_timezone("Москва"), MOSCOW_TIMEZONE)
        self.assertEqual(normalize_event_timezone("мск"), MOSCOW_TIMEZONE)
        self.assertEqual(normalize_event_timezone("Europe/Amsterdam"), "Europe/Amsterdam")
        with self.assertRaises(ValueError):
            normalize_event_timezone("UTC+5")
        with self.assertRaises(ValueError):
            normalize_event_timezone("Not/AZone")

    def test_session_window_parses_start_and_end_in_selected_timezone(self) -> None:
        def parse_local(value: str, *, timezone_name: str) -> str:
            self.assertEqual(timezone_name, "Europe/Moscow")
            if value.endswith("19:00"):
                return "2026-09-25T16:00:00+00:00"
            self.assertTrue(value.endswith("21:30"))
            return "2026-09-25T18:30:00+00:00"

        with patch(
            "clientplatform.application.event_wizard.parse_local_booking_start",
            side_effect=parse_local,
        ):
            session = parse_session_window(
                "25.09.2026 19:00-21:30",
                timezone_name="Europe/Moscow",
                position=3,
            )

        self.assertEqual(session.position, 3)
        self.assertEqual(session.starts_at, datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(session.ends_at, datetime(2026, 9, 25, 18, 30, tzinfo=timezone.utc))
        self.assertEqual(session.local_label, "25.09.2026 19:00–21:30")

    def test_session_window_rejects_bad_or_non_positive_range(self) -> None:
        with self.assertRaises(ValueError):
            parse_session_window(
                "25.09.2026 19:00",
                timezone_name="Europe/Moscow",
                position=1,
            )
        with patch(
            "clientplatform.application.event_wizard.parse_local_booking_start",
            side_effect=(
                "2026-09-25T18:00:00+00:00",
                "2026-09-25T17:00:00+00:00",
            ),
        ):
            with self.assertRaisesRegex(ValueError, "after start"):
                parse_session_window(
                    "25.09.2026 21:00-20:00",
                    timezone_name="Europe/Moscow",
                    position=1,
                )

    def test_sequence_is_chronological_and_non_overlapping(self) -> None:
        first = EventWizardSession(
            position=1,
            starts_at=datetime(2026, 9, 25, 16, tzinfo=timezone.utc),
            ends_at=datetime(2026, 9, 25, 18, tzinfo=timezone.utc),
            local_label="25.09.2026 19:00–21:00",
        )
        second = EventWizardSession(
            position=2,
            starts_at=datetime(2026, 9, 26, 16, tzinfo=timezone.utc),
            ends_at=datetime(2026, 9, 26, 18, tzinfo=timezone.utc),
            local_label="26.09.2026 19:00–21:00",
        )
        self.assertIs(validate_session_sequence(second, previous=first), second)
        overlap = EventWizardSession(
            position=2,
            starts_at=datetime(2026, 9, 25, 17, tzinfo=timezone.utc),
            ends_at=datetime(2026, 9, 25, 19, tzinfo=timezone.utc),
            local_label="25.09.2026 20:00–22:00",
        )
        with self.assertRaisesRegex(ValueError, "overlap"):
            validate_session_sequence(overlap, previous=first)

    def test_join_url_can_arrive_later_but_duplicate_room_is_rejected(self) -> None:
        self.assertIsNone(normalize_session_join_url("-"))
        self.assertEqual(
            normalize_session_join_url("https://room.example/live"),
            "https://room.example/live",
        )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            normalize_session_join_url("http://room.example/live")
        with self.assertRaisesRegex(ValueError, "own room"):
            normalize_session_join_url(
                "https://room.example/live",
                existing_urls=("https://room.example/live",),
            )


if __name__ == "__main__":
    unittest.main()

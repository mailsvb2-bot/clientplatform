from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from clientplatform.application.event_sessions import (
    validate_warmup_days,
    warmup_window_for_sessions,
)
from clientplatform.domain.event_sessions import EventSession


BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
EVENT_ID = "22222222-2222-4222-8222-222222222222"


def _session(*, position: int, starts_at: datetime) -> EventSession:
    return EventSession(
        id=f"33333333-3333-4333-8333-33333333333{position}",
        business_id=BUSINESS_ID,
        event_id=EVENT_ID,
        position=position,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=2),
        provider_key="pending",
        provider_label=None,
        join_url=None,
        created_at=datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
    )


def test_warmup_window_uses_business_calendar_date() -> None:
    now = datetime(2026, 9, 17, 21, 30, tzinfo=timezone.utc)
    first = _session(
        position=1,
        starts_at=datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc),
    )

    window = warmup_window_for_sessions(
        [first],
        timezone_name="Europe/Moscow",
        now=now,
    )

    # In Moscow `now` is already 18 September, while the event is 20 September.
    assert window.days_until_event == 2
    assert window.max_warmup_days == 2
    assert window.timezone_name == "Europe/Moscow"


def test_warmup_window_never_becomes_negative() -> None:
    now = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)
    first = _session(
        position=1,
        starts_at=datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc),
    )

    window = warmup_window_for_sessions(
        [first],
        timezone_name="Europe/Moscow",
        now=now,
    )

    assert window.days_until_event == 0
    assert window.max_warmup_days == 0


def test_warmup_days_cannot_exceed_remaining_window() -> None:
    assert validate_warmup_days(0, max_warmup_days=3) == 0
    assert validate_warmup_days(3, max_warmup_days=3) == 3
    with unittest.TestCase().assertRaisesRegex(ValueError, "exceed"):
        validate_warmup_days(4, max_warmup_days=3)
    with unittest.TestCase().assertRaisesRegex(ValueError, "negative"):
        validate_warmup_days(-1, max_warmup_days=3)

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from clientplatform.application.event_warmups import build_event_warmup_plan


NOW = datetime(2026, 9, 17, 20, 30, tzinfo=timezone.utc)
FIRST_SESSION = datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)


def test_warmup_plan_matches_requested_days_and_stops_before_event() -> None:
    plan = build_event_warmup_plan(
        event_id="event-1",
        title="Практический вебинар",
        description="Разберём заявленную тему на примерах.",
        timezone_name="Europe/Moscow",
        first_session_starts_at=FIRST_SESSION,
        requested_days=3,
        now=NOW,
    )

    assert plan.days_until_event == 5
    assert plan.requested_days == 3
    assert len(plan.drafts) == 3
    assert [draft.days_before_event for draft in plan.drafts] == [3, 2, 1]
    assert [draft.publish_date.isoformat() for draft in plan.drafts] == [
        "2026-09-19",
        "2026-09-20",
        "2026-09-21",
    ]
    assert all("Практический вебинар" in draft.text for draft in plan.drafts)
    assert "Уже завтра" in plan.drafts[-1].text


def test_warmup_plan_rejects_more_days_than_time_remaining() -> None:
    with pytest.raises(ValueError, match="exceed"):
        build_event_warmup_plan(
            event_id="event-1",
            title="Практический вебинар",
            description="",
            timezone_name="Europe/Moscow",
            first_session_starts_at=FIRST_SESSION,
            requested_days=6,
            now=NOW,
        )


def test_warmup_plan_can_be_explicitly_disabled() -> None:
    plan = build_event_warmup_plan(
        event_id="event-1",
        title="Практический вебинар",
        description="",
        timezone_name="Europe/Moscow",
        first_session_starts_at=FIRST_SESSION,
        requested_days=0,
        now=NOW,
    )

    assert plan.requested_days == 0
    assert plan.drafts == ()

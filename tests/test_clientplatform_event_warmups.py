from __future__ import annotations

from contextlib import nullcontext
import sqlite3
from datetime import timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from clientplatform.application import event_warmups as warmups
from clientplatform.application.event_commercial_consent import (
    grant_event_commercial_consent_in_transaction,
)
from clientplatform.application.event_delivery_targets import EventDeliveryTarget
from clientplatform.application.event_owner_flow import (
    MultiSessionOnlineEventCreateRequest,
    OnlineEventSessionCreateRequest,
    create_multisession_online_event_draft_in_transaction,
    publish_multisession_online_event_draft_in_transaction,
)
from clientplatform.infrastructure.event_followup_settings_repository import (
    EventFollowupSettingsRepository,
)
from clientplatform.infrastructure.event_repository import EventRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import create_or_update_tables

import unittest
from datetime import datetime, timezone

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
    assert all(
        draft.scheduled_at.astimezone(ZoneInfo("Europe/Moscow")).hour == 12
        for draft in plan.drafts
    )
    assert all("Практический вебинар" in draft.text for draft in plan.drafts)
    assert "Уже завтра" in plan.drafts[-1].text


def test_warmup_plan_rejects_more_days_than_time_remaining() -> None:
    with unittest.TestCase().assertRaisesRegex(ValueError, "exceed"):
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



class TestPersistedWarmupPlan:
    def setup_method(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        create_or_update_tables(self.conn)
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=501, name="Warmup Business")
        self.actor = tenancy.resolve_context(
            user_id=501,
            business_id=access.business.id,
        )
        starts_at = datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc)
        draft = create_multisession_online_event_draft_in_transaction(
            self.conn,
            actor=self.actor,
            request=MultiSessionOnlineEventCreateRequest(
                title="Практический вебинар",
                description="Разберём тему на примерах.",
                timezone_name="Europe/Moscow",
                enable_email_notifications=False,
                sessions=(
                    OnlineEventSessionCreateRequest(
                        starts_at=starts_at,
                        ends_at=starts_at + timedelta(hours=2),
                        join_url="https://room.example.test/live",
                    ),
                ),
            ),
        )
        publish_multisession_online_event_draft_in_transaction(
            self.conn,
            actor=self.actor,
            event_id=draft.event_id,
        )
        self.event_id = draft.event_id

    def teardown_method(self) -> None:
        self.conn.close()

    def _db_patches(self):
        return (
            patch.object(warmups, "get_db", side_effect=lambda: nullcontext(self.conn)),
            patch.object(warmups, "get_db_ro", side_effect=lambda: nullcontext(self.conn)),
        )

    def test_owner_text_survives_time_and_can_be_reset_without_rescheduling(self) -> None:
        get_db_patch, get_db_ro_patch = self._db_patches()
        with get_db_patch, get_db_ro_patch:
            created = warmups.save_event_warmup_plan(
                actor=self.actor,
                event_id=self.event_id,
                requested_days=3,
                now=datetime(2026, 9, 20, 12, tzinfo=timezone.utc),
            )
            original_schedule = created.drafts[1].scheduled_at
            custom = warmups.set_event_warmup_text(
                actor=self.actor,
                event_id=self.event_id,
                position=2,
                text="Мой собственный прогрев для {name}. {join_url}",
            )
            assert custom.source == "owner"
            assert custom.scheduled_at == original_schedule

            later = warmups.get_saved_event_warmup_plan(
                actor=self.actor,
                event_id=self.event_id,
                now=datetime(2026, 9, 29, 12, tzinfo=timezone.utc),
            )
            assert later.requested_days == 3
            assert later.drafts[1].text.startswith("Мой собственный")
            assert later.drafts[1].source == "owner"
            assert later.drafts[1].scheduled_at == original_schedule

            reset = warmups.reset_event_warmup_text(
                actor=self.actor,
                event_id=self.event_id,
                position=2,
            )
            assert reset.source == "template"
            assert reset.scheduled_at == original_schedule
            assert "Практический вебинар" in reset.text

    def test_due_materializer_sends_only_current_day_and_never_catches_up_yesterday(self) -> None:
        get_db_patch, get_db_ro_patch = self._db_patches()
        with get_db_patch, get_db_ro_patch:
            plan = warmups.save_event_warmup_plan(
                actor=self.actor,
                event_id=self.event_id,
                requested_days=3,
                now=datetime(2026, 9, 20, 12, tzinfo=timezone.utc),
            )
        event = EventRepository(self.conn).get(
            actor=self.actor,
            event_id=self.event_id,
        )
        registration, _created = EventRepository(self.conn).register_public(
            event=event,
            name="Иван",
            email="ivan@example.test",
            phone=None,
            source="telegram",
            campaign_ref="warmup-test",
            consent_version="v1",
            now=datetime(2026, 9, 20, 13, tzinfo=timezone.utc),
        )
        grant_event_commercial_consent_in_transaction(
            self.conn,
            business_id=self.actor.business_id,
            event_id=self.event_id,
            registration_id=registration.id,
            channels=("telegram",),
            now="2026-09-20T13:01:00+00:00",
        )
        EventFollowupSettingsRepository(self.conn).set_enabled(
            business_id=self.actor.business_id,
            enabled=True,
            updated_by_member_id=self.actor.membership_id,
            now="2026-09-20T13:02:00+00:00",
        )
        target = EventDeliveryTarget(
            platform="telegram",
            connection_id="telegram-connection",
            recipient_kind="customer_identity",
            customer_identity_id="identity-1",
            external_subject="501",
        )
        with (
            patch.object(warmups, "_resolve_target", return_value=target),
            patch.object(warmups, "event_commercial_policy_authorized", return_value=True),
            patch.object(warmups, "_public_base_url", return_value="https://clientplatform.example"),
            patch.object(warmups, "_materialize", return_value=True),
        ):
            first_due = plan.drafts[0].scheduled_at + timedelta(minutes=1)
            first = warmups.materialize_due_event_warmups_in_transaction(
                self.conn,
                now=first_due,
            )
            assert first.queued == 1
            assert first.expired == 0

            next_day = plan.drafts[1].scheduled_at + timedelta(minutes=1)
            second = warmups.materialize_due_event_warmups_in_transaction(
                self.conn,
                now=next_day,
            )
            assert second.queued == 1
            # Yesterday is outside the scan window by the time today's noon
            # warmup is due, so stale work cannot crowd out the current day.
            assert second.expired == 0

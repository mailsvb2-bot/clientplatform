from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from clientplatform.application import event_content_plans as plans
from clientplatform.domain.event_content import (
    EventContentMode,
    EventContentStage,
    event_content_mode_label,
    event_content_stage_label,
    event_visual_request,
    parse_event_content_mode,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.infrastructure.event_content_repository import (
    EventContentMessageRepository,
    EventContentPreferenceRepository,
)
from services.db.schema import clientplatform_event_content


BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
OTHER_BUSINESS_ID = "22222222-2222-4222-8222-222222222222"
MEMBER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
EVENT_ID = "33333333-3333-4333-8333-333333333333"


def _actor(*, business_id: str = BUSINESS_ID) -> TenantContext:
    return TenantContext(
        business_id=business_id,
        user_id=101,
        membership_id=MEMBER_ID,
        role=PlatformRole.OWNER,
    )


class EventContentDomainTests(unittest.TestCase):
    def test_owner_labels_and_numeric_shortcuts_are_stable(self) -> None:
        self.assertIs(parse_event_content_mode("1"), EventContentMode.TEXT)
        self.assertIs(parse_event_content_mode("Только текст"), EventContentMode.TEXT)
        self.assertIs(parse_event_content_mode("2"), EventContentMode.TEXT_WITH_IMAGE)
        self.assertIs(
            parse_event_content_mode("Текст + картинка"),
            EventContentMode.TEXT_WITH_IMAGE,
        )
        self.assertIs(parse_event_content_mode("3"), EventContentMode.TEXT_IN_IMAGE)
        self.assertEqual(event_content_mode_label(EventContentMode.TEXT), "Только текст")
        self.assertEqual(
            event_content_stage_label(EventContentStage.POST_EVENT),
            "дожим после мероприятия",
        )
        with self.assertRaises(ValueError):
            parse_event_content_mode("видео")

    def test_visual_request_separates_attachment_and_text_in_image_semantics(self) -> None:
        separate = event_visual_request(
            stage=EventContentStage.WARMUP,
            mode=EventContentMode.TEXT_WITH_IMAGE,
            event_title="Практика",
            message_text="Через два дня встречаемся",
        )
        self.assertIn("без читаемого рекламного текста", separate)
        embedded = event_visual_request(
            stage=EventContentStage.EVENT_DAY,
            mode=EventContentMode.TEXT_IN_IMAGE,
            event_title="Практика",
            message_text="Сегодня в 19:00",
            session_label="25.09.2026 19:00–21:00",
        )
        self.assertIn("хорошо читаемый основной текст", embedded)
        self.assertIn("25.09.2026", embedded)
        with self.assertRaises(ValueError):
            event_visual_request(
                stage=EventContentStage.WARMUP,
                mode=EventContentMode.TEXT,
                event_title="Практика",
                message_text="Текст",
            )

    def test_event_visual_key_is_deterministic_and_validated(self) -> None:
        first = plans.event_visual_idempotency_key(
            event_id=EVENT_ID,
            stage=EventContentStage.WARMUP,
            message_key="warmup-01",
        )
        second = plans.event_visual_idempotency_key(
            event_id=EVENT_ID,
            stage=EventContentStage.WARMUP,
            message_key="warmup-01",
        )
        self.assertEqual(first, second)
        self.assertIn(EVENT_ID, first)
        with self.assertRaises(ValueError):
            plans.event_visual_idempotency_key(
                event_id=EVENT_ID,
                stage=EventContentStage.WARMUP,
                message_key="пробелы нельзя",
            )


class EventContentRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("CREATE TABLE businesses(id TEXT PRIMARY KEY)")
        self.conn.execute(
            "CREATE TABLE business_members(id TEXT, business_id TEXT, PRIMARY KEY(id,business_id))"
        )
        self.conn.execute(
            "CREATE TABLE clientplatform_events(id TEXT, business_id TEXT, PRIMARY KEY(id,business_id))"
        )
        self.conn.execute("INSERT INTO businesses(id) VALUES(?)", (BUSINESS_ID,))
        self.conn.execute("INSERT INTO businesses(id) VALUES(?)", (OTHER_BUSINESS_ID,))
        self.conn.execute(
            "INSERT INTO business_members(id,business_id) VALUES(?,?)",
            (MEMBER_ID, BUSINESS_ID),
        )
        self.conn.execute(
            "INSERT INTO business_members(id,business_id) VALUES(?,?)",
            (MEMBER_ID, OTHER_BUSINESS_ID),
        )
        self.conn.execute(
            "INSERT INTO clientplatform_events(id,business_id) VALUES(?,?)",
            (EVENT_ID, BUSINESS_ID),
        )
        clientplatform_event_content.ensure(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_upsert_is_stage_scoped_and_tenant_scoped(self) -> None:
        repo = EventContentPreferenceRepository(self.conn)
        warmup = repo.set(
            actor=_actor(),
            event_id=EVENT_ID,
            stage=EventContentStage.WARMUP,
            mode=EventContentMode.TEXT_WITH_IMAGE,
            now="2026-09-17T20:00:00+00:00",
        )
        self.assertIs(warmup.mode, EventContentMode.TEXT_WITH_IMAGE)
        updated = repo.set(
            actor=_actor(),
            event_id=EVENT_ID,
            stage=EventContentStage.WARMUP,
            mode=EventContentMode.TEXT_IN_IMAGE,
            now="2026-09-17T20:01:00+00:00",
        )
        self.assertIs(updated.mode, EventContentMode.TEXT_IN_IMAGE)
        self.assertEqual(len(repo.list_for_event(actor=_actor(), event_id=EVENT_ID)), 1)
        with self.assertRaises(ValueError):
            repo.set(
                actor=_actor(business_id=OTHER_BUSINESS_ID),
                event_id=EVENT_ID,
                stage=EventContentStage.WARMUP,
                mode=EventContentMode.TEXT,
            )

    def test_editable_message_slots_preserve_owner_source_and_schedule(self) -> None:
        repo = EventContentMessageRepository(self.conn)
        created = repo.upsert(
            actor=_actor(),
            event_id=EVENT_ID,
            stage=EventContentStage.WARMUP,
            slot_key="day:1",
            position=1,
            text="Автотекст",
            source="template",
            scheduled_at="2026-09-20T09:00:00+00:00",
            now="2026-09-18T10:00:00+00:00",
        )
        self.assertEqual(created.source, "template")
        updated = repo.upsert(
            actor=_actor(),
            event_id=EVENT_ID,
            stage=EventContentStage.WARMUP,
            slot_key="day:1",
            position=1,
            text="Мой собственный прогрев",
            source="owner",
            scheduled_at="2026-09-20T09:00:00+00:00",
            now="2026-09-18T10:05:00+00:00",
        )
        self.assertEqual(updated.text, "Мой собственный прогрев")
        self.assertEqual(updated.source, "owner")
        self.assertEqual(updated.scheduled_at, "2026-09-20T09:00:00+00:00")
        self.assertEqual(
            repo.list_for_stage(
                actor=_actor(),
                event_id=EVENT_ID,
                stage=EventContentStage.WARMUP,
            ),
            (updated,),
        )
        self.assertEqual(
            repo.delete_missing_slots(
                actor=_actor(),
                event_id=EVENT_ID,
                stage=EventContentStage.WARMUP,
                keep_slot_keys=(),
            ),
            1,
        )


class EventContentApplicationTests(unittest.TestCase):
    def test_plan_defaults_missing_stages_to_text(self) -> None:
        repository = MagicMock()
        repository.list_for_event.return_value = ()
        with (
            patch.object(plans, "get_db_ro") as get_db_ro,
            patch.object(plans, "EventContentPreferenceRepository", return_value=repository),
        ):
            get_db_ro.return_value.__enter__.return_value = object()
            plan = plans.get_event_content_plan(actor=_actor(), event_id=EVENT_ID)
        self.assertIs(plan.warmup, EventContentMode.TEXT)
        self.assertIs(plan.event_day, EventContentMode.TEXT)
        self.assertIs(plan.post_event, EventContentMode.TEXT)

    def test_text_mode_never_loads_visual_stack_or_prepares_paid_visual(self) -> None:
        text_plan = plans.EventContentPlan(
            event_id=EVENT_ID,
            warmup=EventContentMode.TEXT,
            event_day=EventContentMode.TEXT,
            post_event=EventContentMode.TEXT,
        )
        with (
            patch.object(plans, "get_event_content_plan", return_value=text_plan),
            patch.object(plans, "_load_goal_visual_brand") as load_brand,
            patch.object(plans, "_freeze_business_image_payload") as freeze,
            patch.object(plans, "prepare_creative_generation") as prepare,
        ):
            result = plans.prepare_event_stage_visual(
                actor=_actor(),
                event_id=EVENT_ID,
                stage=EventContentStage.WARMUP,
                message_key="warmup-01",
                event_title="Практика",
                message_text="Текст",
            )
        self.assertIsNone(result)
        load_brand.assert_not_called()
        freeze.assert_not_called()
        prepare.assert_not_called()

    def test_visual_mode_freezes_receipt_but_does_not_submit_paid_job(self) -> None:
        visual_plan = plans.EventContentPlan(
            event_id=EVENT_ID,
            warmup=EventContentMode.TEXT_WITH_IMAGE,
            event_day=EventContentMode.TEXT,
            post_event=EventContentMode.TEXT,
        )
        brand = SimpleNamespace(prompt_context=lambda: "brand")
        receipt = SimpleNamespace(id="receipt-1", request_text="REQUEST")
        with (
            patch.object(plans, "get_event_content_plan", return_value=visual_plan),
            patch.object(plans, "event_visual_request", return_value="REQUEST"),
            patch.object(plans, "_load_goal_visual_brand", return_value=brand),
            patch.object(plans, "_freeze_business_image_payload", return_value="FROZEN") as freeze,
            patch.object(plans, "prepare_creative_generation", return_value=receipt) as prepare,
        ):
            result = plans.prepare_event_stage_visual(
                actor=_actor(),
                event_id=EVENT_ID,
                stage=EventContentStage.WARMUP,
                message_key="warmup-01",
                event_title="Практика",
                message_text="Текст",
                country_code="RU",
            )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.prepared_for_requested_visual)
        freeze.assert_called_once_with(
            request="REQUEST",
            brand_context="brand",
            country_code="RU",
        )
        prepare.assert_called_once()


if __name__ == "__main__":
    unittest.main()

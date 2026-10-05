from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from clientplatform.application import event_landing_builder
from clientplatform.application.event_public_surface import render_event_landing_body
from clientplatform.application.events import (
    create_event_in_transaction,
    publish_event_in_transaction,
)
from clientplatform.domain.business_profile import BusinessProfileDetails
from clientplatform.domain.event_landing import (
    EventLandingContent,
    EventLandingFaq,
    EventLandingTheme,
    event_landing_content_from_json,
    event_landing_content_to_json,
)
from clientplatform.domain.events import PublicEvent
from clientplatform.domain.tenancy import TenantAccessDenied
from clientplatform.infrastructure.event_landing_repository import (
    EventLandingAIClaim,
    EventLandingRepository,
    get_preview_event_landing,
    get_published_event_landing,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.privacy_manifest import TENANT_POLICIES
from services.db.schema import create_or_update_tables
from services.db.schema import clientplatform_event_content


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    create_or_update_tables(conn)
    return conn


def _owner(conn: sqlite3.Connection, user_id: int, name: str):
    access = TenancyRepository(conn).create_business(owner_user_id=user_id, name=name)
    return TenancyRepository(conn).resolve_context(
        user_id=user_id,
        business_id=access.business.id,
    )


def _published_event(conn: sqlite3.Connection, actor):
    now = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
    event = create_event_in_transaction(
        conn,
        actor=actor,
        title="Как вернуть энергию",
        description=(
            "Практический онлайн-разбор причин перегрузки и способов "
            "выстроить более устойчивый ритм."
        ),
        starts_at=now + timedelta(days=3),
        timezone_name="Europe/Moscow",
        join_url="https://stream.example.test/room",
        provider_key="external",
        now=now,
    )
    return publish_event_in_transaction(
        conn,
        actor=actor,
        event_id=event.id,
        now=now,
    )


def _landing(title: str = "Как вернуть энергию") -> EventLandingContent:
    return EventLandingContent(
        eyebrow="Бесплатный онлайн-вебинар",
        hero_title=title,
        hero_subtitle="Практический разбор без обещаний мгновенного результата.",
        audience_title="Для кого",
        audience_points=("Для тех, кому актуальна тема перегрузки.",),
        outcomes_title="Что разберём",
        outcome_points=(
            "Как заметить источники перегрузки.",
            "Как выстроить следующий шаг.",
        ),
        agenda_title="Программа",
        agenda_points=("Вводная часть", "Практический разбор"),
        speaker_title="Организатор",
        speaker_text="Практика «Метротерапия».",
        faq_title="Вопросы",
        faq=(
            EventLandingFaq(
                question="Как зарегистрироваться?",
                answer="Заполните форму ниже.",
            ),
        ),
        cta_title="Зарегистрироваться",
        cta_text="После регистрации откроется персональная страница.",
        theme=EventLandingTheme.CALM,
    )


class EventLandingDomainTests(unittest.TestCase):
    def test_roundtrip_is_strict_and_bounded(self) -> None:
        landing = _landing()
        restored = event_landing_content_from_json(
            event_landing_content_to_json(landing)
        )
        self.assertEqual(restored, landing)
        self.assertIs(restored.theme, EventLandingTheme.CALM)

        payload = landing.to_payload()
        payload["unknown"] = "nope"
        with self.assertRaisesRegex(ValueError, "unsupported"):
            EventLandingContent.from_payload(payload)

        with self.assertRaisesRegex(ValueError, "at most 6"):
            replace(
                landing,
                audience_points=tuple(
                    f"segment {index}" for index in range(7)
                ),
            )

    def test_manual_section_parsers_are_bounded(self) -> None:
        self.assertEqual(
            event_landing_builder._line_items("Первый\n- Второй"),
            ("Первый", "Второй"),
        )
        faq = event_landing_builder._faq_items(
            "Когда? | Завтра\nГде? — Онлайн"
        )
        self.assertEqual([item.question for item in faq], ["Когда?", "Где?"])
        with self.assertRaisesRegex(ValueError, "формате"):
            event_landing_builder._faq_items("Без разделителя")


class EventLandingRepositoryTests(unittest.TestCase):
    def test_draft_preview_and_publish_remain_separate(self) -> None:
        conn = _conn()
        try:
            actor = _owner(conn, 1001, "Практика")
            event = _published_event(conn, actor)
            repository = EventLandingRepository(conn)
            first = repository.save_draft(
                actor=actor,
                event_id=event.id,
                content=_landing("Черновик 1"),
                source="template",
                now="2026-10-05T09:00:00+00:00",
            )
            self.assertEqual(first.revision, 1)
            self.assertFalse(first.is_published)
            self.assertIsNone(
                get_published_event_landing(
                    conn,
                    public_slug=event.public_slug,
                )
            )

            preview = repository.issue_preview(
                actor=actor,
                event_id=event.id,
                now="2026-10-05T09:01:00+00:00",
            )
            self.assertEqual(preview.revision, 1)
            preview_landing = get_preview_event_landing(
                conn,
                public_slug=event.public_slug,
                token=preview.token,
                now="2026-10-05T09:02:00+00:00",
            )
            self.assertIsNotNone(preview_landing)
            self.assertEqual(preview_landing.hero_title, "Черновик 1")

            published = repository.publish(
                actor=actor,
                event_id=event.id,
                expected_revision=1,
                now="2026-10-05T09:03:00+00:00",
            )
            self.assertTrue(published.is_published)
            self.assertEqual(published.published_revision, 1)

            second = repository.save_draft(
                actor=actor,
                event_id=event.id,
                content=_landing("Черновик 2"),
                source="owner",
                expected_revision=1,
                now="2026-10-05T09:04:00+00:00",
            )
            self.assertEqual(second.revision, 2)
            self.assertTrue(second.has_unpublished_changes)
            public = get_published_event_landing(
                conn,
                public_slug=event.public_slug,
            )
            self.assertIsNotNone(public)
            self.assertEqual(public.hero_title, "Черновик 1")
            self.assertIsNone(
                get_preview_event_landing(
                    conn,
                    public_slug=event.public_slug,
                    token=preview.token,
                    now="2026-10-05T09:05:00+00:00",
                )
            )

            with self.assertRaisesRegex(RuntimeError, "changed"):
                repository.publish(
                    actor=actor,
                    event_id=event.id,
                    expected_revision=1,
                    now="2026-10-05T09:06:00+00:00",
                )
        finally:
            conn.close()

    def test_preview_is_revision_bound_and_expires(self) -> None:
        conn = _conn()
        try:
            actor = _owner(conn, 1002, "Практика")
            event = _published_event(conn, actor)
            repository = EventLandingRepository(conn)
            repository.save_draft(
                actor=actor,
                event_id=event.id,
                content=_landing(),
                source="template",
                now="2026-10-05T09:00:00+00:00",
            )
            preview = repository.issue_preview(
                actor=actor,
                event_id=event.id,
                ttl_seconds=60,
                now="2026-10-05T09:00:00+00:00",
            )
            self.assertIsNotNone(
                get_preview_event_landing(
                    conn,
                    public_slug=event.public_slug,
                    token=preview.token,
                    now="2026-10-05T09:00:59+00:00",
                )
            )
            self.assertIsNone(
                get_preview_event_landing(
                    conn,
                    public_slug=event.public_slug,
                    token=preview.token,
                    now="2026-10-05T09:01:00+00:00",
                )
            )
            self.assertIsNone(
                get_preview_event_landing(
                    conn,
                    public_slug=event.public_slug,
                    token="wrong-token-that-is-long-enough-to-pass-length-check",
                    now="2026-10-05T09:00:30+00:00",
                )
            )
        finally:
            conn.close()

    def test_repository_is_tenant_scoped_and_live_authorized(self) -> None:
        conn = _conn()
        try:
            actor_a = _owner(conn, 1101, "A")
            actor_b = _owner(conn, 1102, "B")
            event_a = _published_event(conn, actor_a)
            repository = EventLandingRepository(conn)
            repository.save_draft(
                actor=actor_a,
                event_id=event_a.id,
                content=_landing(),
                source="owner",
            )
            with self.assertRaisesRegex(ValueError, "active business"):
                repository.get(actor=actor_b, event_id=event_a.id)

            conn.execute(
                "UPDATE business_members SET status='revoked' "
                "WHERE id=? AND business_id=?",
                (actor_a.membership_id, actor_a.business_id),
            )
            with self.assertRaisesRegex(
                TenantAccessDenied,
                "active business membership",
            ):
                repository.get(actor=actor_a, event_id=event_a.id)
        finally:
            conn.close()

    def test_ai_claim_is_idempotent_and_manual_edit_rearms_new_revision(self) -> None:
        conn = _conn()
        try:
            actor = _owner(conn, 1301, "Практика")
            event = _published_event(conn, actor)
            repository = EventLandingRepository(conn)
            repository.save_draft(
                actor=actor,
                event_id=event.id,
                content=_landing(),
                source="template",
                now="2026-10-05T10:00:00+00:00",
            )
            digest = "a" * 64
            first = repository.claim_ai_generation(
                actor=actor,
                event_id=event.id,
                expected_revision=1,
                claim_digest=digest,
                now="2026-10-05T10:01:00+00:00",
            )
            self.assertTrue(first.created)
            duplicate = repository.claim_ai_generation(
                actor=actor,
                event_id=event.id,
                expected_revision=1,
                claim_digest=digest,
                now="2026-10-05T10:01:01+00:00",
            )
            self.assertFalse(duplicate.created)
            self.assertEqual(duplicate.status, "planning")

            repository.mark_ai_generation_ambiguous(
                actor=actor,
                event_id=event.id,
                base_revision=1,
                claim_digest=digest,
                now="2026-10-05T10:02:00+00:00",
            )
            ambiguous = repository.claim_ai_generation(
                actor=actor,
                event_id=event.id,
                expected_revision=1,
                claim_digest=digest,
                now="2026-10-05T10:02:01+00:00",
            )
            self.assertFalse(ambiguous.created)
            self.assertEqual(ambiguous.status, "ambiguous")

            edited = repository.save_draft(
                actor=actor,
                event_id=event.id,
                content=_landing("Новая ревизия"),
                source="owner",
                expected_revision=1,
                now="2026-10-05T10:03:00+00:00",
            )
            self.assertEqual(edited.revision, 2)
            fresh = repository.claim_ai_generation(
                actor=actor,
                event_id=event.id,
                expected_revision=2,
                claim_digest="b" * 64,
                now="2026-10-05T10:04:00+00:00",
            )
            self.assertTrue(fresh.created)
        finally:
            conn.close()

    def test_stale_ai_completion_never_overwrites_newer_owner_draft(self) -> None:
        conn = _conn()
        try:
            actor = _owner(conn, 1302, "Практика")
            event = _published_event(conn, actor)
            repository = EventLandingRepository(conn)
            repository.save_draft(
                actor=actor,
                event_id=event.id,
                content=_landing("До AI"),
                source="template",
            )
            digest = "c" * 64
            repository.claim_ai_generation(
                actor=actor,
                event_id=event.id,
                expected_revision=1,
                claim_digest=digest,
            )
            repository.save_draft(
                actor=actor,
                event_id=event.id,
                content=_landing("Правка владельца"),
                source="owner",
                expected_revision=1,
            )
            with self.assertRaisesRegex(RuntimeError, "changed"):
                repository.complete_ai_generation(
                    actor=actor,
                    event_id=event.id,
                    base_revision=1,
                    claim_digest=digest,
                    content=_landing("Устаревший AI"),
                )
            current = repository.get(actor=actor, event_id=event.id)
            self.assertIsNotNone(current)
            self.assertEqual(current.draft.hero_title, "Правка владельца")
            self.assertEqual(current.revision, 2)
        finally:
            conn.close()

    def test_corrupt_optional_published_landing_falls_back(self) -> None:
        conn = _conn()
        try:
            actor = _owner(conn, 1201, "Практика")
            event = _published_event(conn, actor)
            repository = EventLandingRepository(conn)
            repository.save_draft(
                actor=actor,
                event_id=event.id,
                content=_landing(),
                source="owner",
                now="2026-10-05T10:00:00+00:00",
            )
            repository.publish(
                actor=actor,
                event_id=event.id,
                expected_revision=1,
                now="2026-10-05T10:01:00+00:00",
            )
            conn.execute(
                "UPDATE clientplatform_event_landing_profiles "
                "SET published_json=? WHERE business_id=? AND event_id=?",
                ("{broken-json", actor.business_id, event.id),
            )
            self.assertIsNone(
                get_published_event_landing(
                    conn,
                    public_slug=event.public_slug,
                )
            )
        finally:
            conn.close()


class EventLandingSchemaUpgradeTests(unittest.TestCase):
    def test_existing_landing_table_gains_ai_claim_columns_idempotently(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                """
                CREATE TABLE clientplatform_event_landing_profiles(
                    business_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    draft_json TEXT NOT NULL,
                    published_json TEXT,
                    draft_source TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    published_revision INTEGER,
                    preview_token_digest TEXT,
                    preview_revision INTEGER,
                    preview_expires_at TEXT,
                    updated_by_member_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    published_at TEXT,
                    PRIMARY KEY(business_id, event_id)
                )
                """
            )
            clientplatform_event_content.ensure(conn)
            clientplatform_event_content.ensure(conn)
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(clientplatform_event_landing_profiles)"
                ).fetchall()
            }
            self.assertTrue(
                {
                    "ai_status",
                    "ai_base_revision",
                    "ai_claim_digest",
                    "ai_updated_at",
                }.issubset(columns)
            )
        finally:
            conn.close()


class EventLandingSurfaceTests(unittest.TestCase):
    def test_sales_landing_preserves_registration_and_attribution(self) -> None:
        event = PublicEvent(
            public_slug="A" * 32,
            kind="webinar",
            title="Техническое название",
            description="Описание",
            starts_at=datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc),
            ends_at=None,
            timezone_name="Europe/Moscow",
            provider_key="external",
            provider_label=None,
        )
        landing = replace(
            _landing("<script>alert(1)</script>"),
            hero_subtitle='Оффер "безопасный" <img src=x onerror=alert(1)>',
        )
        body = render_event_landing_body(
            event,
            landing=landing,
            source='yandex_direct" autofocus',
            campaign_ref="campaign<script>",
        )
        self.assertIn("landing-hero", body)
        self.assertIn("Для кого", body)
        self.assertIn("Что разберём", body)
        self.assertIn("Программа", body)
        self.assertIn("Вопросы", body)
        self.assertIn("Регистрация на мероприятие", body)
        self.assertIn(f"/e/{event.public_slug}/register", body)
        self.assertIn("name=source", body)
        self.assertIn("name=campaign_ref", body)
        self.assertIn("yandex_direct&quot; autofocus", body)
        self.assertIn("campaign&lt;script&gt;", body)
        self.assertNotIn("<script>", body)
        self.assertNotIn("<img", body)

    def test_preview_render_disables_registration_submission(self) -> None:
        event = PublicEvent(
            public_slug="B" * 32,
            kind="webinar",
            title="Вебинар",
            description="Описание",
            starts_at=datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc),
            ends_at=None,
            timezone_name="Europe/Moscow",
            provider_key="external",
            provider_label=None,
        )
        body = render_event_landing_body(
            event,
            landing=_landing(),
            registration_enabled=False,
        )
        self.assertNotIn("<form", body)
        self.assertIn("disabled>Зарегистрироваться", body)
        self.assertNotIn(f"/e/{event.public_slug}/register", body)


class EventLandingApplicationTests(unittest.TestCase):
    def test_template_uses_only_event_and_confirmed_business_facts(self) -> None:
        event = SimpleNamespace(
            title="Ресурс и перегрузка",
            description=(
                "Разберём признаки перегрузки. "
                "Обсудим варианты следующего шага."
            ),
            timezone_name="Europe/Moscow",
            local_start_label=lambda: "08.10.2026 18:00",
        )
        sessions = (
            SimpleNamespace(
                position=1,
                starts_at=datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc),
            ),
        )
        details = BusinessProfileDetails(audiences=("Мужчины 35–50 лет",))
        with patch.object(
            event_landing_builder,
            "_event_context",
            return_value=(
                event,
                sessions,
                "Метротерапия",
                SimpleNamespace(
                    activity_description=(
                        "Психологические образовательные программы"
                    )
                ),
                details,
                True,
            ),
        ):
            landing = event_landing_builder.build_event_landing_template(
                actor=SimpleNamespace(),
                event_id="unused",
            )
        self.assertEqual(landing.hero_title, "Ресурс и перегрузка")
        self.assertEqual(
            landing.audience_points,
            ("Мужчины 35–50 лет",),
        )
        self.assertIn("Метротерапия", landing.speaker_text)
        self.assertFalse(
            any(
                "гарант" in item.casefold()
                for item in landing.outcome_points
            )
        )

    def test_editor_projection_open_is_read_only(self) -> None:
        actor = SimpleNamespace()
        template = _landing("Виртуальная автоверсия")
        with (
            patch.object(
                event_landing_builder,
                "get_event_landing_profile",
                return_value=None,
            ),
            patch.object(
                event_landing_builder,
                "build_event_landing_template",
                return_value=template,
            ),
            patch.object(event_landing_builder, "get_db") as write_db,
        ):
            state = event_landing_builder.get_event_landing_editor_state(
                actor=actor,
                event_id="event",
            )
        self.assertEqual(state.draft, template)
        self.assertEqual(state.revision, 0)
        self.assertFalse(state.is_published)
        write_db.assert_not_called()

    def _ai_context(self):
        event = SimpleNamespace(
            kind="webinar",
            title="Ресурс",
            description="Описание",
            timezone_name="Europe/Moscow",
            local_start_label=lambda: "08.10.2026 18:00",
        )
        sessions = (
            SimpleNamespace(
                position=1,
                starts_at=datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc),
            ),
        )
        return (
            event,
            sessions,
            "Бизнес",
            SimpleNamespace(activity_description="Деятельность"),
            BusinessProfileDetails(audiences=("Аудитория",)),
            True,
        )

    def test_ai_generation_uses_one_claimed_call_and_saves_only_draft(self) -> None:
        actor = SimpleNamespace()
        safe = SimpleNamespace(draft=_landing("Безопасный черновик"), revision=1)
        generated = _landing("AI заголовок").to_payload()
        stored = SimpleNamespace(
            draft=_landing("AI заголовок"),
            draft_source="ai",
        )
        client = SimpleNamespace(
            chat=Mock(return_value=json.dumps(generated, ensure_ascii=False))
        )
        repository = Mock()

        def claim(**kwargs):
            return EventLandingAIClaim(
                created=True,
                status="planning",
                base_revision=1,
                claim_digest=kwargs["claim_digest"],
            )

        repository.claim_ai_generation.side_effect = claim
        repository.complete_ai_generation.return_value = stored

        with (
            patch.object(
                event_landing_builder,
                "ensure_event_landing_draft",
                return_value=safe,
            ),
            patch.object(
                event_landing_builder,
                "_event_context",
                return_value=self._ai_context(),
            ),
            patch.object(
                event_landing_builder.OpenAIClient,
                "from_settings",
                return_value=client,
            ),
            patch.object(
                event_landing_builder,
                "get_db",
                side_effect=lambda: nullcontext(object()),
            ),
            patch.object(
                event_landing_builder,
                "EventLandingRepository",
                return_value=repository,
            ),
        ):
            result = event_landing_builder.generate_event_landing_ai(
                actor=actor,
                event_id="event",
            )

        self.assertIs(result, stored)
        self.assertEqual(client.chat.call_count, 1)
        repository.complete_ai_generation.assert_called_once()
        self.assertEqual(result.draft_source, "ai")

    def test_duplicate_ai_claim_never_calls_provider_twice(self) -> None:
        actor = SimpleNamespace()
        safe = SimpleNamespace(draft=_landing(), revision=1)
        client = SimpleNamespace(chat=Mock())
        repository = Mock()
        repository.claim_ai_generation.return_value = EventLandingAIClaim(
            created=False,
            status="planning",
            base_revision=1,
            claim_digest="a" * 64,
        )
        with (
            patch.object(
                event_landing_builder,
                "ensure_event_landing_draft",
                return_value=safe,
            ),
            patch.object(
                event_landing_builder,
                "_event_context",
                return_value=self._ai_context(),
            ),
            patch.object(
                event_landing_builder.OpenAIClient,
                "from_settings",
                return_value=client,
            ),
            patch.object(
                event_landing_builder,
                "get_db",
                side_effect=lambda: nullcontext(object()),
            ),
            patch.object(
                event_landing_builder,
                "EventLandingRepository",
                return_value=repository,
            ),
        ):
            with self.assertRaisesRegex(
                event_landing_builder.EventLandingAIUnavailable,
                "уже создаётся",
            ):
                event_landing_builder.generate_event_landing_ai(
                    actor=actor,
                    event_id="event",
                )
        client.chat.assert_not_called()

    def test_invalid_ai_output_marks_claim_ambiguous_without_draft_write(self) -> None:
        actor = SimpleNamespace()
        safe = SimpleNamespace(draft=_landing("Не менять"), revision=1)
        client = SimpleNamespace(chat=Mock(return_value="{broken-json"))
        repository = Mock()

        def claim(**kwargs):
            return EventLandingAIClaim(
                created=True,
                status="planning",
                base_revision=1,
                claim_digest=kwargs["claim_digest"],
            )

        repository.claim_ai_generation.side_effect = claim
        with (
            patch.object(
                event_landing_builder,
                "ensure_event_landing_draft",
                return_value=safe,
            ),
            patch.object(
                event_landing_builder,
                "_event_context",
                return_value=self._ai_context(),
            ),
            patch.object(
                event_landing_builder.OpenAIClient,
                "from_settings",
                return_value=client,
            ),
            patch.object(
                event_landing_builder,
                "get_db",
                side_effect=lambda: nullcontext(object()),
            ),
            patch.object(
                event_landing_builder,
                "EventLandingRepository",
                return_value=repository,
            ),
        ):
            with self.assertRaisesRegex(
                event_landing_builder.EventLandingAIUnavailable,
                "не изменён",
            ):
                event_landing_builder.generate_event_landing_ai(
                    actor=actor,
                    event_id="event",
                )
        repository.mark_ai_generation_ambiguous.assert_called_once()
        repository.complete_ai_generation.assert_not_called()

    def test_ai_unavailable_never_claims_egress(self) -> None:
        safe = SimpleNamespace(draft=_landing(), revision=1)
        with (
            patch.object(
                event_landing_builder,
                "ensure_event_landing_draft",
                return_value=safe,
            ),
            patch.object(
                event_landing_builder,
                "_event_context",
                return_value=self._ai_context(),
            ),
            patch.object(
                event_landing_builder.OpenAIClient,
                "from_settings",
                return_value=None,
            ),
            patch.object(event_landing_builder, "get_db") as database,
        ):
            with self.assertRaisesRegex(
                event_landing_builder.EventLandingAIUnavailable,
                "не настроена",
            ):
                event_landing_builder.generate_event_landing_ai(
                    actor=SimpleNamespace(),
                    event_id="event",
                )
        database.assert_not_called()


class EventLandingPrivacyTests(unittest.TestCase):
    def test_privacy_manifest_is_explicit(self) -> None:
        policy = TENANT_POLICIES["clientplatform_event_landing_profiles"]
        self.assertEqual(policy.disposition, "retain")
        self.assertIn("participant identity", policy.reason)


if __name__ == "__main__":
    unittest.main()

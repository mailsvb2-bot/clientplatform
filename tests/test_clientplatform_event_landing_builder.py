from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

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
from clientplatform.infrastructure.event_landing_repository import (
    EventLandingRepository,
    get_preview_event_landing,
    get_published_event_landing,
)
from clientplatform.domain.tenancy import TenantAccessDenied
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.privacy_manifest import TENANT_POLICIES
from services.db.schema import create_or_update_tables


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
        description="Практический онлайн-разбор причин перегрузки и способов выстроить более устойчивый ритм.",
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
        outcome_points=("Как заметить источники перегрузки.", "Как выстроить следующий шаг."),
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


def test_event_landing_domain_roundtrip_is_strict_and_bounded() -> None:
    landing = _landing()
    restored = event_landing_content_from_json(event_landing_content_to_json(landing))
    assert restored == landing
    assert restored.theme is EventLandingTheme.CALM

    payload = landing.to_payload()
    payload["unknown"] = "nope"
    with pytest.raises(ValueError, match="unsupported"):
        EventLandingContent.from_payload(payload)

    with pytest.raises(ValueError, match="at most 6"):
        replace(
            landing,
            audience_points=tuple(f"segment {index}" for index in range(7)),
        )


def test_event_landing_repository_keeps_draft_preview_and_publish_separate() -> None:
    conn = _conn()
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
    assert first.revision == 1
    assert first.is_published is False
    assert get_published_event_landing(conn, public_slug=event.public_slug) is None

    preview = repository.issue_preview(
        actor=actor,
        event_id=event.id,
        now="2026-10-05T09:01:00+00:00",
    )
    assert preview.revision == 1
    assert (
        get_preview_event_landing(
            conn,
            public_slug=event.public_slug,
            token=preview.token,
            now="2026-10-05T09:02:00+00:00",
        ).hero_title
        == "Черновик 1"
    )

    published = repository.publish(
        actor=actor,
        event_id=event.id,
        expected_revision=1,
        now="2026-10-05T09:03:00+00:00",
    )
    assert published.is_published is True
    assert published.published_revision == 1
    assert (
        get_published_event_landing(conn, public_slug=event.public_slug).hero_title
        == "Черновик 1"
    )

    second = repository.save_draft(
        actor=actor,
        event_id=event.id,
        content=_landing("Черновик 2"),
        source="owner",
        now="2026-10-05T09:04:00+00:00",
    )
    assert second.revision == 2
    assert second.has_unpublished_changes is True
    assert (
        get_published_event_landing(conn, public_slug=event.public_slug).hero_title
        == "Черновик 1"
    )
    assert (
        get_preview_event_landing(
            conn,
            public_slug=event.public_slug,
            token=preview.token,
            now="2026-10-05T09:05:00+00:00",
        )
        is None
    )

    with pytest.raises(RuntimeError, match="changed"):
        repository.publish(
            actor=actor,
            event_id=event.id,
            expected_revision=1,
            now="2026-10-05T09:06:00+00:00",
        )
    conn.close()


def test_preview_is_revision_bound_and_expires() -> None:
    conn = _conn()
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
    assert get_preview_event_landing(
        conn,
        public_slug=event.public_slug,
        token=preview.token,
        now="2026-10-05T09:00:59+00:00",
    )
    assert (
        get_preview_event_landing(
            conn,
            public_slug=event.public_slug,
            token=preview.token,
            now="2026-10-05T09:01:00+00:00",
        )
        is None
    )
    assert (
        get_preview_event_landing(
            conn,
            public_slug=event.public_slug,
            token="wrong-token-that-is-long-enough-to-pass-length-check",
            now="2026-10-05T09:00:30+00:00",
        )
        is None
    )
    conn.close()


def test_event_landing_repository_is_tenant_scoped_and_live_authorized() -> None:
    conn = _conn()
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
    with pytest.raises(ValueError, match="active business"):
        repository.get(actor=actor_b, event_id=event_a.id)

    conn.execute(
        "UPDATE business_members SET status='revoked' WHERE id=? AND business_id=?",
        (actor_a.membership_id, actor_a.business_id),
    )
    with pytest.raises(TenantAccessDenied, match="active business membership"):
        repository.get(actor=actor_a, event_id=event_a.id)
    conn.close()


def test_public_sales_landing_preserves_canonical_registration_and_attribution() -> None:
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
    assert "landing-hero" in body
    assert "Для кого" in body
    assert "Что разберём" in body
    assert "Программа" in body
    assert "Вопросы" in body
    assert "Регистрация на мероприятие" in body
    assert f"/e/{event.public_slug}/register" in body
    assert "name=source" in body and "name=campaign_ref" in body
    assert "yandex_direct&quot; autofocus" in body
    assert "campaign&lt;script&gt;" in body
    assert "<script>" not in body
    assert "<img" not in body


def test_template_uses_only_event_and_confirmed_business_facts() -> None:
    event = SimpleNamespace(
        title="Ресурс и перегрузка",
        description="Разберём признаки перегрузки. Обсудим варианты следующего шага.",
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
            SimpleNamespace(activity_description="Психологические образовательные программы"),
            details,
            True,
        ),
    ):
        landing = event_landing_builder.build_event_landing_template(
            actor=SimpleNamespace(),
            event_id="unused",
        )
    assert landing.hero_title == "Ресурс и перегрузка"
    assert landing.audience_points == ("Мужчины 35–50 лет",)
    assert "Метротерапия" in landing.speaker_text
    assert not any("гарант" in item.casefold() for item in landing.outcome_points)


def test_ai_generation_saves_only_validated_draft_and_never_publishes() -> None:
    actor = SimpleNamespace()
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
    safe = SimpleNamespace(draft=_landing("Безопасный черновик"))
    config = SimpleNamespace(
        enabled=True,
        provider="openai",
        model="model",
    )
    generated = _landing("AI заголовок").to_payload()
    stored = SimpleNamespace(draft=_landing("AI заголовок"), draft_source="ai")
    repo = SimpleNamespace(save_draft=lambda **kwargs: stored)

    with (
        patch.object(event_landing_builder, "ensure_event_landing_draft", return_value=safe),
        patch.object(
            event_landing_builder,
            "_event_context",
            return_value=(
                event,
                sessions,
                "Бизнес",
                SimpleNamespace(activity_description="Деятельность"),
                BusinessProfileDetails(audiences=("Аудитория",)),
                True,
            ),
        ),
        patch.object(
            event_landing_builder.SalesAIRuntimeConfig,
            "from_env",
            return_value=config,
        ),
        patch.object(
            event_landing_builder,
            "generate_bounded_marketing_json",
            new=AsyncMock(return_value=generated),
        ) as ai,
        patch.object(event_landing_builder, "get_db", return_value=nullcontext(object())),
        patch.object(event_landing_builder, "EventLandingRepository", return_value=repo),
    ):
        result = asyncio.run(
            event_landing_builder.generate_event_landing_ai(
                actor=actor,
                event_id="event",
            )
        )
    assert result is stored
    assert result.draft_source == "ai"
    ai.assert_awaited_once()
    assert "published" not in repo.__dict__


def test_ai_invalid_output_preserves_existing_draft() -> None:
    actor = SimpleNamespace()
    safe = SimpleNamespace(draft=_landing("Не менять"))
    config = SimpleNamespace(enabled=True, provider="openai", model="model")
    with (
        patch.object(event_landing_builder, "ensure_event_landing_draft", return_value=safe),
        patch.object(
            event_landing_builder,
            "_event_context",
            return_value=(
                SimpleNamespace(
                    kind="webinar",
                    title="Ресурс",
                    description="Описание",
                    timezone_name="Europe/Moscow",
                    local_start_label=lambda: "08.10.2026",
                ),
                (),
                "Бизнес",
                SimpleNamespace(activity_description="Деятельность"),
                BusinessProfileDetails(),
                False,
            ),
        ),
        patch.object(
            event_landing_builder.SalesAIRuntimeConfig,
            "from_env",
            return_value=config,
        ),
        patch.object(
            event_landing_builder,
            "generate_bounded_marketing_json",
            new=AsyncMock(return_value={"hero_title": "partial"}),
        ),
        patch.object(event_landing_builder, "get_db") as database,
    ):
        with pytest.raises(
            event_landing_builder.EventLandingAIUnavailable,
            match="не изменён",
        ):
            asyncio.run(
                event_landing_builder.generate_event_landing_ai(
                    actor=actor,
                    event_id="event",
                )
            )
    database.assert_not_called()


def test_manual_section_parsers_are_bounded() -> None:
    assert event_landing_builder._line_items("Первый\n- Второй") == ("Первый", "Второй")
    faq = event_landing_builder._faq_items("Когда? | Завтра\nГде? — Онлайн")
    assert [item.question for item in faq] == ["Когда?", "Где?"]
    with pytest.raises(ValueError, match="формате"):
        event_landing_builder._faq_items("Без разделителя")


def test_event_landing_privacy_manifest_is_explicit() -> None:
    policy = TENANT_POLICIES["clientplatform_event_landing_profiles"]
    assert policy.disposition == "retain"
    assert "participant identity" in policy.rationale


def test_editor_projection_does_not_create_durable_draft_on_open() -> None:
    actor = SimpleNamespace()
    template = _landing("Виртуальная автоверсия")
    with (
        patch.object(event_landing_builder, "get_event_landing_profile", return_value=None),
        patch.object(event_landing_builder, "build_event_landing_template", return_value=template),
        patch.object(event_landing_builder, "get_db") as write_db,
    ):
        state = event_landing_builder.get_event_landing_editor_state(
            actor=actor,
            event_id="event",
        )
    assert state.draft == template
    assert state.revision == 0
    assert state.is_published is False
    write_db.assert_not_called()


def test_preview_render_disables_registration_submission() -> None:
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
    assert "<form" not in body
    assert "disabled>Зарегистрироваться" in body
    assert f"/e/{event.public_slug}/register" not in body


def test_corrupt_optional_published_landing_falls_back_without_breaking_registration() -> None:
    conn = _conn()
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
        "UPDATE clientplatform_event_landing_profiles SET published_json=? "
        "WHERE business_id=? AND event_id=?",
        ("{broken-json", actor.business_id, event.id),
    )
    assert get_published_event_landing(conn, public_slug=event.public_slug) is None
    conn.close()


def test_landing_draft_creation_is_idempotent_and_stale_write_fails_closed() -> None:
    conn = _conn()
    actor = _owner(conn, 1301, "Практика")
    event = _published_event(conn, actor)
    repository = EventLandingRepository(conn)

    first = repository.ensure_draft(
        actor=actor,
        event_id=event.id,
        content=_landing("Первая автоверсия"),
        source="template",
        now="2026-10-05T11:00:00+00:00",
    )
    repeated = repository.ensure_draft(
        actor=actor,
        event_id=event.id,
        content=_landing("Не должна затереть первую"),
        source="template",
        now="2026-10-05T11:00:01+00:00",
    )
    assert first.revision == repeated.revision == 1
    assert repeated.draft.hero_title == "Первая автоверсия"

    updated = repository.save_draft(
        actor=actor,
        event_id=event.id,
        content=_landing("Свежая ручная правка"),
        source="owner",
        expected_revision=1,
        now="2026-10-05T11:01:00+00:00",
    )
    assert updated.revision == 2

    with pytest.raises(RuntimeError, match="changed concurrently"):
        repository.save_draft(
            actor=actor,
            event_id=event.id,
            content=_landing("Устаревший AI результат"),
            source="ai",
            expected_revision=1,
            now="2026-10-05T11:02:00+00:00",
        )

    current = repository.get(actor=actor, event_id=event.id)
    assert current is not None
    assert current.revision == 2
    assert current.draft.hero_title == "Свежая ручная правка"
    conn.close()

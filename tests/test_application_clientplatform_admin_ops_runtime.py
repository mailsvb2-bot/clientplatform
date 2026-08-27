from __future__ import annotations

from dataclasses import replace

import pytest

from clientplatform.application.activity import (
    create_business_offering,
    enable_business_capability,
    save_business_profile,
)
from clientplatform.application.admin_ops import (
    InteractionMetricInput,
    create_publication_draft,
    format_publication_calendar_lines,
    get_subscription_state,
    interaction_snapshot,
    list_offering_prices,
    list_open_alerts,
    list_payments,
    list_publication_calendar,
    list_publications,
    publish_publication,
    record_interaction_metric,
    record_payment,
    refresh_interaction_alerts,
    set_offering_price,
    toggle_autopilot,
)
from clientplatform.application.customers import create_customer
from clientplatform.application.tenancy import (
    create_business,
    grant_business_member,
    resolve_tenant_context,
)
from clientplatform.domain.tenancy import PlatformRole, TenantPermissionDenied
from services.db import get_db


def _business(user_id: int, name: str):
    access = create_business(owner_user_id=user_id, name=name)
    actor = resolve_tenant_context(
        user_id=user_id,
        business_id=access.business.id,
    )
    save_business_profile(
        actor=actor,
        activity_description=f"Деятельность {name}",
        timezone_name="Europe/Moscow",
    )
    capability = enable_business_capability(
        actor=actor,
        connector_key="services",
    )
    offering = create_business_offering(
        actor=actor,
        capability_id=capability.id,
        title="Консультация",
        description="Практическая консультация",
    )
    customer = create_customer(actor=actor, display_name="Тестовый клиент")
    return actor, offering, customer


def test_admin_operations_are_real_and_tenant_isolated() -> None:
    actor, offering, customer = _business(801001, "Первый бизнес")
    other, _, _ = _business(801002, "Второй бизнес")

    draft = create_publication_draft(
        actor=actor,
        title="Полезная публикация",
        body="Полный текст публикации",
        channel="telegram",
    )
    assert draft.status == "draft"
    assert publish_publication(
        actor=actor,
        publication_id=draft.id,
    ).status == "published"

    payment = record_payment(
        actor=actor,
        customer_id=customer.id,
        amount_minor=350_000,
        idempotency_key="admin-runtime-first-payment",
        currency="RUB",
        note="Консультация",
    )
    assert payment.customer_id == customer.id
    assert payment.amount_minor == 350_000

    price = set_offering_price(
        actor=actor,
        offering_id=offering.id,
        amount_minor=500_000,
        currency="RUB",
    )
    assert price.offering_id == offering.id
    assert price.amount_minor == 500_000

    assert len(list_publications(actor=actor)) == 1
    assert len(list_payments(actor=actor)) == 1
    assert len(list_offering_prices(actor=actor)) == 1
    assert list_publications(actor=other) == []
    assert list_payments(actor=other) == []
    assert list_offering_prices(actor=other) == []


def test_role_permissions_remain_fail_closed() -> None:
    actor, _, _ = _business(802001, "Роли")
    member = grant_business_member(
        actor=actor,
        user_id=802002,
        role=PlatformRole.CONTENT_MANAGER,
    )
    content_actor = resolve_tenant_context(
        user_id=member.user_id,
        business_id=actor.business_id,
    )

    create_publication_draft(
        actor=content_actor,
        title="Черновик контент-менеджера",
        body="Текст",
    )
    assert len(list_publications(actor=content_actor)) == 1

    with pytest.raises(TenantPermissionDenied):
        record_payment(
            actor=content_actor,
            amount_minor=100_00,
            idempotency_key="admin-runtime-denied-payment",
            currency="RUB",
        )


def test_publication_calendar_projects_existing_facts_in_business_timezone() -> None:
    actor, _, _ = _business(802101, "Контент-календарь")
    other, _, _ = _business(802102, "Чужой контент")
    scheduled = create_publication_draft(
        actor=actor,
        title="План на завтра",
        body="Запланированный материал",
        channel="vk",
    )
    published = create_publication_draft(
        actor=actor,
        title="Уже опубликовано",
        body="Готовый материал",
        channel="telegram",
    )
    failed = create_publication_draft(
        actor=actor,
        title="Нужно проверить",
        body="Материал с ошибкой",
        channel="max",
    )

    with get_db() as conn:
        conn.execute(
            """
            UPDATE business_publications
            SET status='scheduled', scheduled_at=?, updated_at=?
            WHERE id=? AND business_id=?
            """,
            (
                "2026-08-28T09:00:00+00:00",
                "2026-08-27T10:00:00+00:00",
                scheduled.id,
                actor.business_id,
            ),
        )
        conn.execute(
            """
            UPDATE business_publications
            SET status='published', published_at=?, updated_at=?
            WHERE id=? AND business_id=?
            """,
            (
                "2026-08-27T12:00:00+00:00",
                "2026-08-27T12:00:00+00:00",
                published.id,
                actor.business_id,
            ),
        )
        conn.execute(
            """
            UPDATE business_publications
            SET status='failed', failed_at=?, failure_reason=?, updated_at=?
            WHERE id=? AND business_id=?
            """,
            (
                "2026-08-27T11:00:00+00:00",
                "provider retry required",
                "2026-08-27T11:00:00+00:00",
                failed.id,
                actor.business_id,
            ),
        )

    calendar = list_publication_calendar(actor=actor)
    assert [item.id for item in calendar] == [scheduled.id, published.id, failed.id]
    assert calendar[0].scheduled_at == "2026-08-28T09:00:00+00:00"
    assert calendar[2].failed_at == "2026-08-27T11:00:00+00:00"
    assert calendar[2].failure_reason == "provider retry required"
    assert list_publication_calendar(actor=other) == []

    lines = format_publication_calendar_lines(
        calendar,
        timezone_name="Europe/Moscow",
    )
    assert "28.08.2026 12:00 · ВКонтакте · Запланировано · План на завтра" in lines[0]
    assert "27.08.2026 15:00 · Telegram · Опубликовано" in lines[1]
    assert "27.08.2026 14:00 · MAX · Ошибка" in lines[2]


def test_publication_calendar_formatter_is_bounded_and_fail_safe() -> None:
    actor, _, _ = _business(802103, "Границы календаря")
    draft = create_publication_draft(
        actor=actor,
        title="Очень длинный заголовок публикации для проверки компактного отображения владельцу",
        body="Текст",
        channel="other",
    )
    with get_db() as conn:
        conn.execute(
            "UPDATE business_publications SET updated_at=? WHERE id=? AND business_id=?",
            ("not-a-time", draft.id, actor.business_id),
        )
    calendar = list_publication_calendar(actor=actor)
    lines = format_publication_calendar_lines(
        calendar,
        timezone_name="Invalid/Timezone",
        max_entries=1,
    )
    assert len(lines) == 1
    assert "время не указано" in lines[0]
    assert "Другой канал · Черновик" in lines[0]
    assert lines[0].endswith("…")

    zulu = replace(
        calendar[0],
        title="UTC fallback",
        updated_at="2026-08-27T12:00:00Z",
    )
    assert "27.08.2026 12:00" in format_publication_calendar_lines(
        [zulu],
        timezone_name="Invalid/Timezone",
    )[0]
    naive = replace(
        calendar[0],
        status="cancelled",
        title="Отменено владельцем",
        updated_at="2026-08-27T13:00:00",
    )
    naive_line = format_publication_calendar_lines(
        [naive],
        timezone_name="UTC",
    )[0]
    assert "27.08.2026 13:00 · Другой канал · Отменено" in naive_line
    assert format_publication_calendar_lines([], timezone_name="UTC") == (
        "• Публикаций пока нет.",
    )
    with pytest.raises(ValueError):
        format_publication_calendar_lines(calendar, timezone_name="UTC", max_entries=0)
    with pytest.raises(ValueError):
        format_publication_calendar_lines(calendar, timezone_name="UTC", max_entries=True)


def test_interaction_metrics_have_exact_percentiles_and_stable_alert_counts() -> None:
    actor, _, _ = _business(803001, "Метрики")

    for value in range(1, 101):
        record_interaction_metric(
            InteractionMetricInput(
                business_id=actor.business_id,
                actor_user_id=actor.user_id,
                callback_action="today",
                success=value != 100,
                ack_ms=10,
                lock_wait_ms=5,
                app_ms=value // 2,
                telegram_ms=value // 2,
                total_ms=value,
                transport_role="ui",
                transport_route="149.154.167.220",
                transport_generation=0,
                error_code=None if value != 100 else "TimeoutError",
            )
        )

    snapshot = interaction_snapshot(actor=actor, window_minutes=60)
    assert snapshot.count == 100
    assert snapshot.successes == 99
    assert snapshot.failures == 1
    assert snapshot.p50_ms == 50
    assert snapshot.p95_ms == 95
    assert snapshot.max_ms == 100

    first = refresh_interaction_alerts(
        actor=actor,
        p95_warning_ms=80,
        failure_percent_warning=0.5,
        route_redundant=False,
    )
    second = refresh_interaction_alerts(
        actor=actor,
        p95_warning_ms=80,
        failure_percent_warning=0.5,
        route_redundant=False,
    )
    assert {item.kind for item in first} == {
        "interaction_failures",
        "interaction_latency",
        "telegram_route_redundancy",
    }
    assert {item.kind: item.occurrences for item in second} == {
        "interaction_failures": 1,
        "interaction_latency": 1,
        "telegram_route_redundancy": 1,
    }

    assert refresh_interaction_alerts(
        actor=actor,
        p95_warning_ms=1000,
        failure_percent_warning=100,
        route_redundant=True,
    ) == []
    assert list_open_alerts(actor=actor) == []


def test_growth_autopilot_generates_actionable_recommendations() -> None:
    actor, offering, _ = _business(804001, "Автопилот")

    assert toggle_autopilot(actor=actor) is True
    alerts = refresh_interaction_alerts(
        actor=actor,
        route_redundant=True,
    )
    kinds = {item.kind for item in alerts}
    assert "growth_unpriced_offerings" in kinds
    assert "growth_no_publications" in kinds
    assert "growth_customers_without_program" in kinds

    set_offering_price(
        actor=actor,
        offering_id=offering.id,
        amount_minor=250_000,
    )
    publication = create_publication_draft(
        actor=actor,
        title="Публикация",
        body="Текст",
    )
    publish_publication(actor=actor, publication_id=publication.id)
    refreshed = refresh_interaction_alerts(
        actor=actor,
        route_redundant=True,
    )
    refreshed_kinds = {item.kind for item in refreshed}
    assert "growth_unpriced_offerings" not in refreshed_kinds
    assert "growth_no_publications" not in refreshed_kinds
    assert "growth_customers_without_program" in refreshed_kinds


def test_subscription_state_is_persistent_and_owner_only() -> None:
    actor, _, _ = _business(805001, "Тариф")
    first = get_subscription_state(actor=actor)
    second = get_subscription_state(actor=actor)
    assert first == second
    assert first.status == "active"
    assert first.included_staff >= 1

    member = grant_business_member(
        actor=actor,
        user_id=805002,
        role=PlatformRole.MANAGER,
    )
    manager = resolve_tenant_context(
        user_id=member.user_id,
        business_id=actor.business_id,
    )
    with pytest.raises(TenantPermissionDenied):
        get_subscription_state(actor=manager)

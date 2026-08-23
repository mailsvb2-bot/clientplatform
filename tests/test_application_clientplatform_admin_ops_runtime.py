from __future__ import annotations

import pytest

from clientplatform.application.activity import (
    create_business_offering,
    enable_business_capability,
    save_business_profile,
)
from clientplatform.application.admin_ops import (
    InteractionMetricInput,
    create_publication_draft,
    get_subscription_state,
    interaction_snapshot,
    list_offering_prices,
    list_open_alerts,
    list_payments,
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

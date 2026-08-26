from __future__ import annotations

from uuid import uuid4

from clientplatform.application import sales_ui
from clientplatform.domain.sales import SalesLeadStage
from clientplatform.domain.tenancy import TenantContext


def _actor() -> TenantContext:
    return TenantContext(
        business_id=str(uuid4()),
        member_id=str(uuid4()),
        role="owner",
    )


def test_assign_sales_work_delegates_to_canonical_operation(monkeypatch) -> None:
    actor = _actor()
    lead_id = str(uuid4())
    member_id = str(uuid4())
    sentinel = object()
    seen: dict[str, object] = {}

    def fake_assign_sales_lead(**kwargs):
        seen.update(kwargs)
        return sentinel

    monkeypatch.setattr(sales_ui, "assign_sales_lead", fake_assign_sales_lead)

    result = sales_ui.assign_sales_work(
        actor=actor,
        lead_id=lead_id,
        member_id=member_id,
    )

    assert result is sentinel
    assert seen == {"actor": actor, "lead_id": lead_id, "member_id": member_id}


def test_sales_work_mutations_share_same_actor_context(monkeypatch) -> None:
    actor = _actor()
    lead_id = str(uuid4())
    calls: list[tuple[str, dict[str, object]]] = []
    sentinel = object()

    monkeypatch.setattr(
        sales_ui,
        "set_sales_next_action",
        lambda **kwargs: calls.append(("next", kwargs)) or sentinel,
    )
    monkeypatch.setattr(
        sales_ui,
        "add_sales_note",
        lambda **kwargs: calls.append(("note", kwargs)) or True,
    )
    monkeypatch.setattr(
        sales_ui,
        "transition_sales_lead",
        lambda **kwargs: calls.append(("stage", kwargs)) or sentinel,
    )

    assert (
        sales_ui.set_sales_work_next_action(
            actor=actor,
            lead_id=lead_id,
            next_action="Позвонить клиенту",
            due_at="2026-08-27T09:00:00+03:00",
        )
        is sentinel
    )
    assert sales_ui.add_sales_work_note(
        actor=actor,
        lead_id=lead_id,
        note="Клиент попросил связаться утром",
        dedupe_key="vk:message:42",
    )
    assert (
        sales_ui.transition_sales_work(
            actor=actor,
            lead_id=lead_id,
            stage=SalesLeadStage.QUALIFIED,
            reason="confirmed_need",
        )
        is sentinel
    )

    assert [name for name, _ in calls] == ["next", "note", "stage"]
    assert all(call["actor"] is actor for _, call in calls)
    assert all(call["lead_id"] == lead_id for _, call in calls)

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from clientplatform.application import native_member_interactions as ui
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.customer_interactions import CustomerInteractionMessage
from clientplatform.domain.sales import SalesLeadStage
from clientplatform.domain.tenancy import PlatformRole, TenantContext


def _actor() -> TenantContext:
    return TenantContext(
        business_id=str(uuid4()),
        membership_id=str(uuid4()),
        user_id=101,
        role=PlatformRole.OWNER,
    )


def _item(lead_id: str) -> dict[str, object]:
    return {
        "id": lead_id,
        "customer_name": "Анна",
        "stage": "qualified",
        "assigned_user_id": 101,
        "next_action": "Позвонить",
        "due_at": "2026-08-27T09:00:00+00:00",
        "attribution_source": "yandex_direct",
        "attribution_source_ref_id": "campaign:neutral-runtime",
        "closure_reason": None,
        "next_plan_id": None,
    }


@pytest.mark.parametrize("platform", [ConnectionPlatform.VK, ConnectionPlatform.MAX])
def test_native_sales_card_is_transport_neutral(monkeypatch, platform) -> None:
    actor = _actor()
    lead_id = str(uuid4())
    monkeypatch.setattr(ui, "get_sales_workspace_item", lambda **_: _item(lead_id))
    message = ui._render(
        actor,
        ui.ParsedMemberInteraction("sales-lead", (lead_id,)),
        linked=False,
        setup_issuer=None,
        setup_key=f"test:{platform.value}",
        current_platform=platform,
    )
    assert "Анна" in message.text
    assert "Источник: yandex_direct · campaign:neutral-runtime" in message.text
    commands = [button.command for row in message.rows for button in row]
    assert f"cpm:sales-unassign:{lead_id}" in commands
    assert f"cpm:sales-stage:{lead_id}:checkout" in commands


def test_vk_and_max_assign_through_same_workspace_operation(monkeypatch) -> None:
    actor = _actor()
    lead_id = str(uuid4())
    calls: list[tuple[TenantContext, str]] = []
    monkeypatch.setattr(
        ui,
        "assign_sales_workspace_to_actor",
        lambda *, actor, lead_id: calls.append((actor, lead_id)) or SimpleNamespace(),
    )
    monkeypatch.setattr(ui, "get_sales_workspace_item", lambda **_: _item(lead_id))
    for platform in (ConnectionPlatform.VK, ConnectionPlatform.MAX):
        ui._render(
            actor,
            ui.ParsedMemberInteraction("sales-assign", (lead_id,)),
            linked=False,
            setup_issuer=None,
            setup_key=f"assign:{platform.value}",
            current_platform=platform,
        )
    assert calls == [(actor, lead_id), (actor, lead_id)]


def test_native_close_reason_reaches_canonical_transition(monkeypatch) -> None:
    actor = _actor()
    lead_id = str(uuid4())
    short_id = lead_id[:8]
    parsed = ui.parse_native_member_interaction(
        f"результат {short_id} потеряно бюджет выше ожиданий"
    )
    assert parsed.action == "sales-close-text"
    monkeypatch.setattr(ui, "_sales_reference_item", lambda *_args, **_kwargs: {"id": lead_id})
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        ui,
        "transition_sales_workspace",
        lambda **kwargs: calls.append(kwargs) or SimpleNamespace(),
    )
    monkeypatch.setattr(
        ui,
        "_sales_lead_message",
        lambda *_args, **_kwargs: CustomerInteractionMessage(text="Карточка обновлена"),
    )
    message = ui._sales_mutation_message(
        actor,
        parsed,
        interaction_key="route:event:close",
    )
    assert message.text == "Карточка обновлена"
    assert calls == [
        {
            "actor": actor,
            "lead_id": lead_id,
            "stage": SalesLeadStage.LOST,
            "reason": "бюджет выше ожиданий",
        }
    ]

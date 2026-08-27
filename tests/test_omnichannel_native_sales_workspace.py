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
        "source_kind": "vk",
        "contact_basis": "consent",
        "attribution_source": "yandex_direct",
        "attribution_source_ref_id": "campaign:neutral-runtime",
        "active_followup_id": None,
        "active_followup_scheduled_at": None,
        "followup_suppressed": False,
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
    assert "Источник: Яндекс Директ" in message.text
    assert "campaign:neutral-runtime" not in message.text
    commands = [button.command for row in message.rows for button in row]
    assert commands == [
        f"cpm:sales-stage:{lead_id}:checkout",
        f"cpm:sales-actions:{lead_id}",
        "cpm:sales",
    ]

    advanced = ui._sales_actions_message(actor, lead_id)
    advanced_commands = [button.command for row in advanced.rows for button in row]
    assert f"cpm:sales-unassign:{lead_id}" in advanced_commands
    assert f"cpm:sales-stage:{lead_id}:checkout" in advanced_commands
    assert f"cpm:sales-followup-menu:{lead_id}" in advanced_commands


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


def test_growth_keeps_acquisition_and_navigation_with_button_limit() -> None:
    message = ui._growth_message(_actor())
    commands = [button.command for row in message.rows for button in row]
    assert "cpm:acquire" in commands
    assert "cpm:menu" in commands
    assert len(commands) <= 10


def _prepared_acquisition() -> SimpleNamespace:
    destinations = (
        SimpleNamespace(platform=ConnectionPlatform.VK),
        SimpleNamespace(platform=ConnectionPlatform.MAX),
    )
    destination = SimpleNamespace(
        public_url="https://client.example.test/clientplatform/acquire?source=cpa_source123",
        messenger_destinations=destinations,
        has_native_messenger_destination=True,
    )
    creative = SimpleNamespace(
        headline="Консультация",
        primary_text="Есть свободное время.",
    )
    promotion = SimpleNamespace(
        campaign=SimpleNamespace(creative=creative),
        slot=SimpleNamespace(local_start="27.08.2026 12:00", offering_title="Встреча"),
    )
    return SimpleNamespace(promotion=promotion, destination=destination)


def test_native_acquisition_is_same_for_vk_and_max(monkeypatch) -> None:
    actor = _actor()
    monkeypatch.setattr(
        ui.settings,
        "MESSENGER_PUBLIC_BASE_URL",
        "https://client.example.test",
    )
    monkeypatch.setattr(
        ui,
        "prepare_nearest_acquisition_destination",
        lambda **_: _prepared_acquisition(),
    )
    messages = []
    for platform in (ConnectionPlatform.VK, ConnectionPlatform.MAX):
        messages.append(
            ui._render(
                actor,
                ui.ParsedMemberInteraction("acquire"),
                linked=False,
                setup_issuer=None,
                setup_key=f"acquire:{platform.value}",
                current_platform=platform,
            )
        )
    assert messages[0].text == messages[1].text
    assert "https://client.example.test/clientplatform/acquire?source=cpa_source123" in messages[0].text
    assert "Доступно клиенту: ВКонтакте, MAX" in messages[0].text
    assert "платная реклама не запускается" in messages[0].text


def test_native_acquisition_without_slot_is_fail_safe(monkeypatch) -> None:
    actor = _actor()
    monkeypatch.setattr(
        ui.settings,
        "MESSENGER_PUBLIC_BASE_URL",
        "https://client.example.test",
    )
    monkeypatch.setattr(ui, "prepare_nearest_acquisition_destination", lambda **_: None)
    message = ui._acquisition_message(actor)
    assert "Сначала нужно открыть хотя бы одно будущее время" in message.text
    commands = [button.command for row in message.rows for button in row]
    assert "cpm:bookings" in commands
    assert "cpm:growth" in commands


def test_native_sales_card_progressively_discloses_followup_without_losing_it(monkeypatch) -> None:
    actor = _actor()
    lead_id = str(uuid4())
    monkeypatch.setattr(ui, "get_sales_workspace_item", lambda **_: _item(lead_id))
    card = ui._sales_lead_message(actor, lead_id)
    card_commands = [button.command for row in card.rows for button in row]
    assert f"cpm:sales-followup-menu:{lead_id}" not in card_commands
    assert f"cpm:sales-actions:{lead_id}" in card_commands
    assert len(card_commands) <= 3

    actions = ui._sales_actions_message(actor, lead_id)
    action_commands = [button.command for row in actions.rows for button in row]
    assert f"cpm:sales-followup-menu:{lead_id}" in action_commands
    assert len(action_commands) <= 10


def test_native_followup_schedule_is_transport_neutral(monkeypatch) -> None:
    actor = _actor()
    lead_id = str(uuid4())
    parsed = ui.parse_native_member_interaction(
        f"напомнить {lead_id[:8]} 24 Напомнить о встрече завтра"
    )
    assert parsed.action == "sales-followup-text"
    monkeypatch.setattr(ui, "_sales_reference_item", lambda *_a, **_k: {"id": lead_id})
    monkeypatch.setattr(
        ui,
        "_sales_lead_message",
        lambda *_a, **_k: CustomerInteractionMessage(text="Карточка обновлена"),
    )
    calls: list[dict[str, object]] = []

    def schedule(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(scheduled_at="2026-08-28T09:00:00+00:00")

    monkeypatch.setattr(ui, "schedule_sales_workspace_followup", schedule)
    for platform in (ConnectionPlatform.VK, ConnectionPlatform.MAX):
        message = ui._render(
            actor,
            parsed,
            linked=False,
            setup_issuer=None,
            setup_key=f"followup:{platform.value}",
            current_platform=platform,
        )
        assert "Напоминание клиенту запланировано" in message.text
    assert len(calls) == 2
    for call in calls:
        assert call["actor"] == actor
        assert call["lead_id"] == lead_id
        assert call["message_text"] == "Напомнить о встрече завтра"
        assert call["hours_from_now"] == 24
        assert "platform" not in call


def test_native_followup_optout_requires_explicit_confirmation(monkeypatch) -> None:
    actor = _actor()
    lead_id = str(uuid4())
    parsed = ui.parse_native_member_interaction(
        f"не писать {lead_id[:8]} подтвердить"
    )
    assert parsed.action == "sales-followup-optout-text"
    monkeypatch.setattr(ui, "_sales_reference_item", lambda *_a, **_k: {"id": lead_id})
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        ui,
        "suppress_sales_workspace_followup",
        lambda **kwargs: calls.append(kwargs) or 1,
    )
    monkeypatch.setattr(
        ui,
        "_sales_lead_message",
        lambda *_a, **_k: CustomerInteractionMessage(text="Карточка обновлена"),
    )
    message = ui._sales_mutation_message(
        actor,
        parsed,
        interaction_key="route:event:optout",
    )
    assert "Запрет на follow-up сохранён" in message.text
    assert calls == [{"actor": actor, "lead_id": lead_id}]


def test_native_handoff_queue_is_same_for_vk_and_max(monkeypatch) -> None:
    actor = _actor()
    handoff_id = str(uuid4())
    monkeypatch.setattr(
        ui,
        "list_sales_workspace_handoffs",
        lambda **_: [{
            "id": handoff_id,
            "customer_name": "Анна",
            "reason": "explicit_request",
            "severity": "urgent",
            "status": "open",
        }],
    )
    messages = []
    for platform in (ConnectionPlatform.VK, ConnectionPlatform.MAX):
        messages.append(ui._render(
            actor,
            ui.ParsedMemberInteraction("sales-handoffs"),
            linked=False,
            setup_issuer=None,
            setup_key=f"handoff:{platform.value}",
            current_platform=platform,
        ))
    assert messages[0].text == messages[1].text
    commands = [button.command for row in messages[0].rows for button in row]
    assert f"cpm:sales-handoff-claim:{handoff_id}" in commands
    assert f"cpm:sales-handoff-resolve:{handoff_id}" in commands
    assert len(commands) <= 10


def test_native_handoff_claim_delegates_to_shared_workspace(monkeypatch) -> None:
    actor = _actor()
    handoff_id = str(uuid4())
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        ui,
        "claim_sales_workspace_handoff",
        lambda **kwargs: calls.append(kwargs) or {},
    )
    monkeypatch.setattr(
        ui,
        "_sales_handoffs_message",
        lambda *_a, **_k: CustomerInteractionMessage(text="Очередь обновлена"),
    )
    for platform in (ConnectionPlatform.VK, ConnectionPlatform.MAX):
        message = ui._render(
            actor,
            ui.ParsedMemberInteraction("sales-handoff-claim", (handoff_id,)),
            linked=False,
            setup_issuer=None,
            setup_key=f"handoff-claim:{platform.value}",
            current_platform=platform,
        )
        assert message.text == "Очередь обновлена"
    assert calls == [
        {"actor": actor, "handoff_id": handoff_id},
        {"actor": actor, "handoff_id": handoff_id},
    ]


def test_lost_native_lead_can_reopen_through_canonical_workspace(monkeypatch) -> None:
    actor = _actor()
    lead_id = str(uuid4())
    lost = _item(lead_id)
    lost["stage"] = "lost"
    lost["closure_reason"] = "бюджет"
    monkeypatch.setattr(ui, "get_sales_workspace_item", lambda **_: lost)
    card = ui._sales_lead_message(actor, lead_id)
    commands = [button.command for row in card.rows for button in row]
    assert f"cpm:sales-reopen:{lead_id}" in commands
    assert f"cpm:sales-actions:{lead_id}" in commands

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        ui,
        "reopen_sales_workspace",
        lambda **kwargs: calls.append(kwargs) or SimpleNamespace(),
    )
    monkeypatch.setattr(
        ui,
        "_sales_lead_message",
        lambda *_a, **_k: CustomerInteractionMessage(text="Снова в работе"),
    )
    message = ui._sales_mutation_message(
        actor,
        ui.ParsedMemberInteraction("sales-reopen", (lead_id,)),
        interaction_key="route:event:reopen",
    )
    assert message.text == "Снова в работе"
    assert calls == [{"actor": actor, "lead_id": lead_id}]

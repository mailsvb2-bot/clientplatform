from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.application import native_member_interactions as native_ui
from clientplatform.domain.customer_interactions import CustomerInteractionMessage
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.presentation.event_ui import (
    BACK_TO_EVENTS_LABEL,
    BACK_TO_GROWTH_LABEL,
    event_creation_prompt,
    event_creation_success_text,
    event_hub_actions,
    event_hub_text,
    event_settings_actions,
    event_settings_text,
)

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_MEMBER = "22222222-2222-4222-8222-222222222222"


def _actor() -> TenantContext:
    return TenantContext(
        business_id=_BUSINESS,
        user_id=101,
        membership_id=_MEMBER,
        role=PlatformRole.OWNER,
    )


def _snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        items=(
            SimpleNamespace(
                title="Вебинар",
                local_start="15.09.2026 19:00",
                registered=10,
                join_clicked=8,
                attendance_confirmed=6,
                offer_clicked=4,
                paid=2,
                revenue=(SimpleNamespace(display="10 000 RUB"),),
            ),
        ),
        commercial_followups_enabled=False,
        commercial_followups_effective=False,
        commercial_followups_platform_available=True,
        commercial_followup_segments=(
            "no_show",
            "join_signal_unpaid",
            "attended_unpaid",
            "offer_clicked_unpaid",
        ),
        commercial_followup_channels=("email", "max", "vk"),
        can_manage=True,
        can_enable_commercial_followups=True,
        can_expand_commercial_followups=True,
        limitations=(),
    )


def _commands(message: CustomerInteractionMessage) -> list[tuple[str, str]]:
    return [(button.label, button.command) for row in message.rows for button in row]


def test_event_hub_semantics_are_single_source_for_all_messengers() -> None:
    snapshot = _snapshot()
    hub_text = event_hub_text(snapshot)
    hub_labels = [action.label for action in event_hub_actions(snapshot)]
    settings_text = event_settings_text(snapshot)
    settings_labels = [action.label for action in event_settings_actions(snapshot)]
    assert "🎥 Вебинары" in hub_text
    assert "регистрации 10 · пришли 6 · оплаты 2" in hub_text
    assert "Зарегистрировались, но не пришли" not in hub_text
    assert hub_labels == ["🎥 Создать вебинар", "⚙️ Автосообщения"]
    assert "⚙️ Автосообщения после вебинара" in settings_text
    assert "Зарегистрировались, но не пришли" in settings_text
    assert "Вошли в эфир, участие не подтверждено" in settings_text
    assert "🟢 Включить автосообщения" in settings_labels
    assert "✅ MAX" in settings_labels
    assert "✅ VK" in settings_labels


def test_progressive_disclosure_preserves_full_webinar_automation_power() -> None:
    snapshot = _snapshot()
    hub = event_hub_actions(snapshot)
    settings = event_settings_actions(snapshot)

    assert [action.kind for action in hub] == ["create", "settings"]
    assert any(action.kind == "followups" for action in settings)
    assert {action.key for action in settings if action.kind == "segment"} == {
        "no_show",
        "join_signal_unpaid",
        "attended_unpaid",
        "offer_clicked_unpaid",
    }
    assert {action.key for action in settings if action.kind == "channel"} == {
        "email",
        "max",
        "vk",
    }


def test_shared_event_action_labels_fit_native_transport_limit_without_truncation() -> None:
    active = _snapshot()
    inactive = SimpleNamespace(**{**vars(active), "commercial_followup_segments": (), "commercial_followup_channels": ()})
    for snapshot in (active, inactive):
        for action in (*event_hub_actions(snapshot), *event_settings_actions(snapshot)):
            assert len(action.label) <= 40, action.label
            assert native_ui._button(action.label, "cpm:events").label == action.label


def test_vk_and_max_event_hub_render_identically_before_transport() -> None:
    actor = _actor()
    snapshot = _snapshot()
    with (
        patch.object(native_ui, "_business_name", return_value="Бизнес"),
        patch.object(native_ui, "resolve_events_snapshot", return_value=snapshot),
    ):
        base = native_ui._events_message(actor)
    vk = native_ui._with_parent_navigation(base, native_ui.ParsedMemberInteraction("events"))
    max_ui = native_ui._with_parent_navigation(base, native_ui.ParsedMemberInteraction("events"))
    assert vk == max_ui
    commands = _commands(vk)
    assert ("🎥 Создать вебинар", "cpm:event-new") in commands
    assert ("⚙️ Автосообщения", "cpm:event-settings") in commands
    assert (BACK_TO_GROWTH_LABEL, "cpm:growth") in commands


def test_event_settings_and_mutations_return_to_webinar_hub() -> None:
    hub = CustomerInteractionMessage(
        text="settings",
        rows=((native_ui._button("✅ VK", "cpm:event-channel:vk:off"),), native_ui._back_row()),
    )
    for parsed in (
        native_ui.ParsedMemberInteraction("event-settings"),
        native_ui.ParsedMemberInteraction("event-channel", ("vk", "on")),
    ):
        rendered = native_ui._with_parent_navigation(hub, parsed)
        commands = _commands(rendered)
        assert (BACK_TO_EVENTS_LABEL, "cpm:events") in commands
        assert not any(command == "cpm:growth" and label == BACK_TO_EVENTS_LABEL for label, command in commands)


def test_vk_events_projection_failure_is_not_reported_as_stale_button() -> None:
    actor = _actor()
    with (
        patch.object(native_ui, "resolve_tenant_context", return_value=actor),
        patch.object(native_ui, "resolve_events_snapshot", side_effect=ValueError("broken projection")),
    ):
        rendered = native_ui.render_native_member_interaction(
            actor=actor,
            raw_text="cpm:events",
            interaction_key="vk:test:events",
            current_platform=native_ui.ConnectionPlatform.VK,
        )
    assert "Эта кнопка уже неактуальна" not in rendered.text
    assert "Раздел открыт" in rendered.text
    assert ("🎥 Создать вебинар", "cpm:event-new") in _commands(rendered)


def test_event_creation_copy_normalizes_external_provider_and_matches_webinar_vocabulary() -> None:
    prompt = event_creation_prompt("Europe/Moscow")
    assert "🎥 Новый вебинар" in prompt
    assert "Europe/Moscow" in prompt
    assert "Отмена" in prompt
    assert BACK_TO_EVENTS_LABEL in prompt
    success = event_creation_success_text(
        title="Эфир",
        local_time="15.09.2026 19:00",
        provider_key="external",
        registration_url="https://example.test/e/demo",
        email_notifications_enabled=False,
    )
    assert "✅ Вебинар опубликован" in success
    assert "внешняя площадка" in success
    assert "external" not in success
    assert "E-mail не подключён" in success

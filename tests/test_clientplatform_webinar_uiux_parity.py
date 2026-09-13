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
    text = event_hub_text(snapshot)
    labels = [action.label for action in event_hub_actions(snapshot)]
    assert "🎥 Вебинары" in text
    assert "Зарегистрировались, но не пришли" in text
    assert "Перешли к эфиру, участие не подтверждено" in text
    assert labels[0] == "🎥 Создать вебинар"
    assert "🟢 Включить автосообщения" in labels
    assert "✅ MAX" in labels
    assert "✅ VK" in labels


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
    assert (BACK_TO_GROWTH_LABEL, "cpm:growth") in _commands(vk)


def test_event_mutation_refresh_does_not_navigate_back_to_itself() -> None:
    hub = CustomerInteractionMessage(
        text="hub",
        rows=((native_ui._button("🎥 Создать вебинар", "cpm:event-new"),), native_ui._back_row()),
    )
    rendered = native_ui._with_parent_navigation(hub, native_ui.ParsedMemberInteraction("event-channel", ("vk", "on")))
    commands = _commands(rendered)
    assert (BACK_TO_GROWTH_LABEL, "cpm:growth") in commands
    assert not any(command == "cpm:events" and label == BACK_TO_GROWTH_LABEL for label, command in commands)


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

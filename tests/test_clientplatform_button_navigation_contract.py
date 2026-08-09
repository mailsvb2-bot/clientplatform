from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from handlers import clientplatform_control as control
from handlers.clientplatform_interaction_safety import (
    ClientPlatformInteractionSafetyMiddleware,
    _callback_conflicts_with_state,
    _callback_should_clear_state,
    _is_repeatable_navigation,
)


def _message(*, user_id: int = 71) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Button audit"),
        text="screen",
    )


def _callback(data: str, *, user_id: int = 71) -> CallbackQuery:
    message = _message(user_id=user_id)
    assert message.from_user is not None
    return CallbackQuery(
        id=f"button-audit-{user_id}-{data}",
        from_user=message.from_user,
        chat_instance="button-audit",
        message=message,
        data=data,
    )


def _state(*, user_id: int = 71) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


def test_advertising_wizard_buttons_are_allowed_only_in_their_own_step() -> None:
    assert not _callback_conflicts_with_state(
        "AdConnectionState:selecting_connection",
        "cpa:conn:0",
    )
    assert _callback_conflicts_with_state(
        "AdConnectionState:selecting_connection",
        "cpa:campaign:0",
    )
    assert not _callback_conflicts_with_state(
        "AdConnectionState:selecting_campaign",
        "cpa:campaign:0",
    )
    assert _callback_conflicts_with_state(
        "AdConnectionState:selecting_campaign",
        "cpa:conn:0",
    )
    assert not _callback_conflicts_with_state(
        "AdConnectionState:confirming_publication",
        "cpa:confirm",
    )
    assert _callback_conflicts_with_state(
        "AdConnectionState:confirming_publication",
        "cpa:campaign:0",
    )


def test_advertising_cancel_navigation_clears_stale_wizard_state() -> None:
    for state_name in (
        "AdConnectionState:selecting_connection",
        "AdConnectionState:selecting_campaign",
        "AdConnectionState:waiting_regions",
        "AdConnectionState:confirming_publication",
    ):
        assert not _callback_conflicts_with_state(state_name, "cpa:home:business")
        assert _callback_should_clear_state(state_name, "cpa:home:business")


def test_yandex_oauth_can_only_leave_through_its_explicit_cancel() -> None:
    state_name = "YandexScreenCodeState:waiting_code"
    assert not _callback_conflicts_with_state(
        state_name,
        "cpa:yandex-cancel:business",
    )
    assert not _callback_should_clear_state(
        state_name,
        "cpa:yandex-cancel:business",
    )
    assert _callback_conflicts_with_state(state_name, "cpj:home:business")
    assert _callback_conflicts_with_state(state_name, "cpy:a:business:30")


def test_booking_wizard_buttons_remain_inside_booking_state() -> None:
    state_name = "ClientPlatformControlState:booking_duration"
    for data in (
        "cpj:wizdur:business:30",
        "cpj:wizcustom:business",
        "cpj:wizback:business",
        "cpj:wizcancel:business",
    ):
        assert not _callback_conflicts_with_state(state_name, data)
        assert not _callback_should_clear_state(state_name, data)

    assert not _callback_conflicts_with_state(
        "ClientPlatformControlState:booking_start",
        "cpj:wizcancel:business",
    )


def test_first_result_cancel_escapes_control_and_program_builders() -> None:
    for state_name in (
        "ClientPlatformControlState:offering_title",
        "ClientPlatformControlState:booking_start",
        "ClientPlatformProgramBuilderState:program_title",
    ):
        assert not _callback_conflicts_with_state(
            state_name,
            "cps:cancelsetup:business",
        )
        assert _callback_should_clear_state(
            state_name,
            "cps:cancelsetup:business",
        )


def test_program_draft_review_actions_are_not_blocked_by_safety_layer() -> None:
    state_name = "ClientPlatformProgramBuilderState:review"
    for data in (
        "cp:dadd:business:program",
        "cp:dpub:business:program",
        "cp:darc:business:program",
    ):
        assert not _callback_conflicts_with_state(state_name, data)
        assert not _callback_should_clear_state(state_name, data)


def test_sales_cancel_buttons_escape_text_steps() -> None:
    assert not _callback_conflicts_with_state(
        "ClientPlatformSalesUiState:ladder_name",
        "cps:sl:business",
    )
    assert _callback_should_clear_state(
        "ClientPlatformSalesUiState:ladder_name",
        "cps:sl:business",
    )
    assert not _callback_conflicts_with_state(
        "ClientPlatformSalesUiState:ladder_step_title",
        "cps:slv:business:ladder",
    )
    assert _callback_should_clear_state(
        "ClientPlatformSalesUiState:ladder_step_title",
        "cps:slv:business:ladder",
    )


def test_sensitive_bot_and_admin_inputs_reject_unrelated_old_keyboards() -> None:
    for state_name in (
        "ManagedBotSetupState:username",
        "ExistingBotSetupState:token",
        "ClientPlatformSafetyState:business_name",
        "ClientPlatformAdminState:waiting_member_user",
    ):
        assert _callback_conflicts_with_state(state_name, "cpj:home:business")

    assert not _callback_conflicts_with_state(
        "ManagedBotSetupState:username",
        "cpb:b:business",
    )
    assert not _callback_conflicts_with_state(
        "ManagedBotSetupState:username",
        "cpb:c:business:request",
    )
    assert not _callback_conflicts_with_state(
        "ClientPlatformSafetyState:business_name",
        "cps:cancel:business",
    )


def test_spend_consent_keeps_confirmation_state_but_home_is_explicit_cancel() -> None:
    state_name = "AdSpendConsentState:confirming_consent"
    assert not _callback_conflicts_with_state(
        state_name,
        "cpsp:confirm:business:authorization",
    )
    assert not _callback_should_clear_state(
        state_name,
        "cpsp:confirm:business:authorization",
    )
    assert not _callback_conflicts_with_state(state_name, "cpsp:home:business")
    assert _callback_should_clear_state(state_name, "cpsp:home:business")
    assert _callback_conflicts_with_state(state_name, "cpj:home:business")


def test_admin_ops_back_button_remains_available_inside_text_flow() -> None:
    for state_name in (
        "ClientPlatformAdminOpsState:publication_title",
        "ClientPlatformAdminOpsState:publication_body",
        "ClientPlatformAdminOpsState:payment_value",
        "ClientPlatformAdminOpsState:price_value",
    ):
        assert not _callback_conflicts_with_state(
            state_name,
            "cpao:business:return-payments",
        )


def test_ordinary_navigation_can_escape_unknown_builder_but_mutation_cannot() -> None:
    state_name = "OtherBuilderState:title"
    assert not _callback_conflicts_with_state(state_name, "cpj:home:business")
    assert _callback_should_clear_state(state_name, "cpj:home:business")
    assert _callback_conflicts_with_state(state_name, "cp:book:business:slot")
    assert _callback_conflicts_with_state(state_name, "cpb:n:business")


def test_admin_menu_is_recognized_as_token_first_navigation() -> None:
    data = "cpa:abcdefghijklmnopqrstuv:menu"
    assert _is_repeatable_navigation(data)
    assert not _callback_conflicts_with_state(
        "ClientPlatformControlState:activity_description",
        data,
    )
    assert _callback_should_clear_state(
        "ClientPlatformControlState:activity_description",
        data,
    )


@pytest.mark.asyncio
async def test_repeatable_navigation_clears_stale_state_and_is_never_deduped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_answers: list[str | None] = []
    handled_states: list[str | None] = []

    async def answer_callback(
        _callback: CallbackQuery,
        text: str | None = None,
        **_kwargs: Any,
    ) -> None:
        callback_answers.append(text)

    async def handler(_event: Any, data: dict[str, Any]) -> str:
        fsm = data["state"]
        handled_states.append(await fsm.get_state())
        return "handled"

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)

    middleware = ClientPlatformInteractionSafetyMiddleware()
    state = _state()
    await state.set_state(control.ClientPlatformControlState.activity_description)
    callback = _callback("cpj:calendar:business:30")
    data = {"bot": type("Bot", (), {"id": 1})(), "state": state}

    assert await middleware(handler, callback, data) == "handled"
    assert await middleware(handler, callback, data) == "handled"
    assert handled_states == [None, None]
    assert "Действие уже выполняется." not in callback_answers


@pytest.mark.asyncio
async def test_mutating_callback_still_has_double_tap_protection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_answers: list[str | None] = []
    handled = 0

    async def answer_callback(
        _callback: CallbackQuery,
        text: str | None = None,
        **_kwargs: Any,
    ) -> None:
        callback_answers.append(text)

    async def edit_reply_markup(
        _message: Message,
        **_kwargs: Any,
    ) -> None:
        return None

    async def handler(_event: Any, _data: dict[str, Any]) -> str:
        nonlocal handled
        handled += 1
        return "handled"

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    monkeypatch.setattr(Message, "edit_reply_markup", edit_reply_markup)

    middleware = ClientPlatformInteractionSafetyMiddleware()
    callback = _callback("cp:book:business:slot")
    data = {"bot": type("Bot", (), {"id": 1})(), "state": _state()}

    assert await middleware(handler, callback, data) == "handled"
    assert await middleware(handler, callback, data) is None
    assert handled == 1
    assert callback_answers[-1] == "Действие уже выполняется."

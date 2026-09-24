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


def test_advertising_wizard_buttons_work_only_in_the_step_that_rendered_them() -> None:
    assert not _callback_conflicts_with_state(
        "AdConnectionState:selecting_connection", "cpa:conn:0"
    )
    assert _callback_conflicts_with_state(
        "AdConnectionState:selecting_connection", "cpa:campaign:0"
    )
    assert not _callback_conflicts_with_state(
        "AdConnectionState:selecting_campaign", "cpa:campaign:0"
    )
    assert _callback_conflicts_with_state(
        "AdConnectionState:selecting_campaign", "cpa:conn:0"
    )
    assert not _callback_conflicts_with_state(
        "AdConnectionState:confirming_publication", "cpa:confirm"
    )


def test_advertising_home_exits_every_nonsecret_wizard_step() -> None:
    for state_name in (
        "AdConnectionState:selecting_connection",
        "AdConnectionState:selecting_campaign",
        "AdConnectionState:waiting_regions",
        "AdConnectionState:confirming_publication",
    ):
        assert not _callback_conflicts_with_state(state_name, "cpa:home:business")
        assert _callback_should_clear_state(state_name, "cpa:home:business")


def test_yandex_oauth_is_fail_closed_except_for_its_explicit_cancel() -> None:
    state_name = "YandexScreenCodeState:waiting_code"
    assert not _callback_conflicts_with_state(state_name, "cpa:yandex-cancel:business")
    assert not _callback_should_clear_state(state_name, "cpa:yandex-cancel:business")
    assert _callback_conflicts_with_state(state_name, "cpj:home:business")
    assert _callback_conflicts_with_state(state_name, "cpy:a:business:30")


def test_booking_wizard_keeps_its_buttons_inside_booking_state() -> None:
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
        "ClientPlatformControlState:booking_start", "cpj:wizcancel:business"
    )


def test_first_result_cancel_really_exits_owner_setup() -> None:
    for state_name in (
        "ClientPlatformControlState:offering_title",
        "ClientPlatformControlState:booking_start",
        "ClientPlatformProgramBuilderState:program_title",
    ):
        assert not _callback_conflicts_with_state(state_name, "cps:cancelsetup:business")
        assert _callback_should_clear_state(state_name, "cps:cancelsetup:business")


def test_program_draft_and_lesson_navigation_remains_usable() -> None:
    for data in (
        "cp:dadd:business:program",
        "cp:dpub:business:program",
        "cp:darc:business:program",
        "cp:dless:business:program:0",
    ):
        assert not _callback_conflicts_with_state(
            "ClientPlatformProgramBuilderState:review", data
        )
        assert not _callback_should_clear_state(
            "ClientPlatformProgramBuilderState:review", data
        )
    for state_name in (
        "ClientPlatformDraftLessonEditorState:title",
        "ClientPlatformDraftLessonEditorState:content",
    ):
        assert not _callback_conflicts_with_state(
            state_name, "cp:dlcancel:business:lesson"
        )
        assert not _callback_should_clear_state(
            state_name, "cp:dlcancel:business:lesson"
        )


def test_cloud_material_picker_accepts_only_its_current_step_buttons() -> None:
    assert not _callback_conflicts_with_state(
        "ClientPlatformCloudMediaState:choose_kind", "cpcm:k:video"
    )
    assert _callback_conflicts_with_state(
        "ClientPlatformCloudMediaState:choose_kind", "cpcm:s:cloud"
    )
    assert not _callback_conflicts_with_state(
        "ClientPlatformCloudMediaState:choose_source", "cpcm:s:cloud"
    )
    assert _callback_conflicts_with_state(
        "ClientPlatformCloudMediaState:choose_source", "cpcm:k:video"
    )


def test_sales_cancel_buttons_exit_text_steps_instead_of_deadlocking() -> None:
    pairs = (
        ("ClientPlatformSalesUiState:ladder_name", "cps:sl:business"),
        (
            "ClientPlatformSalesUiState:ladder_step_title",
            "cps:slv:business:ladder",
        ),
    )
    for state_name, data in pairs:
        assert not _callback_conflicts_with_state(state_name, data)
        assert _callback_should_clear_state(state_name, data)


def test_sensitive_bot_admin_and_spend_inputs_reject_unrelated_old_keyboards() -> None:
    for state_name in (
        "ManagedBotSetupState:username",
        "ExistingBotSetupState:token",
        "ClientPlatformSafetyState:business_name",
        "ClientPlatformAdminState:waiting_member_user",
        "AdSpendConsentState:confirming_consent",
    ):
        assert _callback_conflicts_with_state(state_name, "cpj:home:business")

    assert not _callback_conflicts_with_state(
        "ManagedBotSetupState:username", "cpb:b:business"
    )
    assert not _callback_conflicts_with_state(
        "ClientPlatformSafetyState:business_name", "cps:cancel:business"
    )
    assert not _callback_conflicts_with_state(
        "AdSpendConsentState:confirming_consent",
        "cpsp:confirm:business:authorization",
    )
    assert not _callback_conflicts_with_state(
        "AdSpendConsentState:confirming_consent", "cpsp:home:business"
    )
    assert _callback_should_clear_state(
        "AdSpendConsentState:confirming_consent", "cpsp:home:business"
    )


def test_admin_ops_back_buttons_are_available_inside_text_flow() -> None:
    for state_name in (
        "ClientPlatformAdminOpsState:publication_title",
        "ClientPlatformAdminOpsState:publication_body",
        "ClientPlatformAdminOpsState:payment_value",
        "ClientPlatformAdminOpsState:price_value",
    ):
        assert not _callback_conflicts_with_state(
            state_name, "cpao:business:return-payments"
        )


def test_managed_bot_lifecycle_refresh_is_navigation_but_mutations_are_not() -> None:
    assert _is_repeatable_navigation("cpbl:o:business:bot")
    assert not _is_repeatable_navigation("cpbl:dx:business:bot")
    assert not _is_repeatable_navigation("cpbl:ax:business:bot")
    assert not _is_repeatable_navigation("cpbl:rx:business:bot")
    assert not _callback_conflicts_with_state(
        "ClientPlatformControlState:activity_description", "cpbl:o:business:bot"
    )
    assert _callback_should_clear_state(
        "ClientPlatformControlState:activity_description", "cpbl:o:business:bot"
    )


def test_ordinary_navigation_escapes_stale_builder_but_mutation_does_not() -> None:
    state_name = "OtherBuilderState:title"
    assert not _callback_conflicts_with_state(state_name, "cpj:home:business")
    assert _callback_should_clear_state(state_name, "cpj:home:business")
    assert _callback_conflicts_with_state(state_name, "cp:book:business:slot")
    assert _callback_conflicts_with_state(state_name, "cpb:n:business")


def test_sales_lead_detail_is_safe_repeatable_navigation_from_stale_state() -> None:
    data = "cps:swv:business-token:lead-token"
    state_name = "ClientPlatformControlState:activity_description"
    assert _is_repeatable_navigation(data)
    assert not _callback_conflicts_with_state(state_name, data)
    assert _callback_should_clear_state(state_name, data)


def test_admin_token_first_menu_is_recognized_as_repeatable_navigation() -> None:
    data = "cpa:abcdefghijklmnopqrstuv:menu"
    assert _is_repeatable_navigation(data)
    assert not _callback_conflicts_with_state(
        "ClientPlatformControlState:activity_description", data
    )
    assert _callback_should_clear_state(
        "ClientPlatformControlState:activity_description", data
    )


def test_webinar_schedule_edit_enters_from_ordinary_state_but_not_sensitive_state() -> None:
    data = "cpev:edit:event-token:business-token"
    ordinary = "ClientPlatformControlState:activity_description"
    assert not _is_repeatable_navigation(data)
    assert not _callback_conflicts_with_state(ordinary, data)
    assert _callback_should_clear_state(ordinary, data)
    assert _callback_conflicts_with_state("ManagedBotSetupState:username", data)


def test_webinar_calendar_callbacks_are_repeatable_and_never_hit_duplicate_toast() -> None:
    for data in (
        "cpev:noop",
        "cpev:month:202610",
        "cpev:month:202611",
        "cpev:date:2026-11-20",
    ):
        assert _is_repeatable_navigation(data), data


def test_telegram_webinar_lifecycle_accepts_current_step_callbacks() -> None:
    allowed = {
        "ClientPlatformEventLifecycleState:waiting_title": (
            "cpev:new:business",
            "cpev:cancel:business",
        ),
        "ClientPlatformEventLifecycleState:waiting_description": (
            "cpev:cancel:business",
        ),
        "ClientPlatformEventLifecycleState:waiting_days": (
            "cpev:cancel:business",
        ),
        "ClientPlatformEventLifecycleState:waiting_timezone": (
            "cpev:tz:moscow",
            "cpev:tz:other",
            "cpev:cancel:business",
        ),
        "ClientPlatformEventLifecycleState:waiting_session_time": (
            "cpev:venue:telemost",
            "cpev:noop",
            "cpev:month:202610",
            "cpev:date:2026-10-01",
            "cpev:start:1900",
            "cpev:duration:120",
            "cpev:manual-time",
            "cpev:cancel:business",
        ),
        "ClientPlatformEventLifecycleState:waiting_session_url": (
            "cpev:cancel:business",
        ),
        "ClientPlatformEventLifecycleState:waiting_offer": (
            "cpev:cancel:business",
        ),
        "ClientPlatformEventLifecycleState:waiting_confirmation": (
            "cpev:confirm:create",
            "cpev:confirm:edit",
            "cpev:cancel:business",
        ),
        "ClientPlatformEventLifecycleState:waiting_warmup_days": (
            "cpev:cancel:business",
        ),
        "ClientPlatformEventLifecycleState:waiting_warmup_mode": (
            "cpev:cancel:business",
        ),
        "ClientPlatformEventLifecycleState:waiting_event_day_mode": (
            "cpev:cancel:business",
        ),
        "ClientPlatformEventLifecycleState:waiting_post_event_mode": (
            "cpev:cancel:business",
        ),
    }
    for state_name, callbacks in allowed.items():
        for callback in callbacks:
            assert not _callback_conflicts_with_state(state_name, callback), (
                state_name,
                callback,
            )
            assert not _callback_should_clear_state(state_name, callback), (
                state_name,
                callback,
            )

    assert _callback_conflicts_with_state(
        "ClientPlatformEventLifecycleState:waiting_timezone",
        "cpev:confirm:create",
    )
    assert _callback_conflicts_with_state(
        "ClientPlatformEventLifecycleState:waiting_confirmation",
        "cpev:venue:telemost",
    )


@pytest.mark.asyncio
async def test_telegram_webinar_timezone_button_is_not_blocked_by_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[tuple[str | None, bool]] = []
    handled: list[str] = []

    async def answer_callback(
        _callback: CallbackQuery,
        text: str | None = None,
        *,
        show_alert: bool = False,
        **_kwargs: Any,
    ) -> None:
        answers.append((text, show_alert))

    async def handler(event: Any, _data: dict[str, Any]) -> str:
        handled.append(str(event.data))
        return "handled"

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    middleware = ClientPlatformInteractionSafetyMiddleware()
    state = _state()
    await state.set_state("ClientPlatformEventLifecycleState:waiting_timezone")
    callback = _callback("cpev:tz:moscow")
    data = {"bot": type("Bot", (), {"id": 1})(), "state": state}

    assert await middleware(handler, callback, data) == "handled"
    assert handled == ["cpev:tz:moscow"]
    assert ("Сначала завершите текущий шаг или отправьте /cancel.", True) not in answers


def test_native_webinar_wizard_buttons_escape_only_ordinary_stale_fsm() -> None:
    ordinary = "ClientPlatformControlState:activity_description"
    timezone = "cpm:event-wizard:timezone:moscow"
    custom_timezone = "cpm:event-wizard:timezone:other"

    assert not _callback_conflicts_with_state(ordinary, timezone)
    assert _callback_should_clear_state(ordinary, timezone)
    assert not _callback_conflicts_with_state(ordinary, custom_timezone)
    assert _callback_should_clear_state(ordinary, custom_timezone)

    assert _callback_conflicts_with_state("ManagedBotSetupState:username", timezone)
    assert _callback_conflicts_with_state("AdSpendConsentState:confirming_consent", timezone)


def test_owner_group_navigation_escapes_stale_ordinary_wizards() -> None:
    state_name = "ClientPlatformControlState:activity_description"
    for data in (
        "cpo:more:business",
        "cpo:clients:business",
        "cpo:content:business",
        "cpo:settings:business",
        "cpo:entrypoints:business",
        "cpo:website:business",
        "cpo:sources:business",
        "cpo:integrations:business",
        "cpo:business-more:business",
        "cpo:work:business",
        "cpo:ads:business",
    ):
        assert _is_repeatable_navigation(data)
        assert not _callback_conflicts_with_state(state_name, data)
        assert _callback_should_clear_state(state_name, data)


def test_service_ad_selection_callbacks_are_guarded_and_step_local() -> None:
    start = "cpo:start:business"
    ordinary = "ClientPlatformControlState:activity_description"
    assert not _is_repeatable_navigation(start)
    assert not _callback_conflicts_with_state(ordinary, start)
    assert _callback_should_clear_state(ordinary, start)
    for data in (
        "cpo:offer:business:offering",
        "cpo:newtime:business:offering",
        "cpo:slot:business:slot",
        "cpo:connection:0",
        "cpo:region:47",
    ):
        assert not _is_repeatable_navigation(data), data

    assert not _callback_conflicts_with_state(
        "OneClickOwnerState:selecting_connection",
        "cpo:connection:0",
    )
    assert _callback_conflicts_with_state(
        "OneClickOwnerState:selecting_connection",
        "cpo:region:47",
    )
    assert not _callback_conflicts_with_state(
        "OneClickOwnerState:waiting_region",
        "cpo:region:47",
    )
    assert _callback_conflicts_with_state(
        "OneClickOwnerState:waiting_region",
        "cpo:slot:business:slot",
    )


@pytest.mark.asyncio
async def test_native_webinar_timezone_callback_clears_stale_fsm_and_reaches_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[tuple[str | None, bool]] = []
    handled_states: list[str | None] = []

    async def answer_callback(
        _callback: CallbackQuery,
        text: str | None = None,
        *,
        show_alert: bool = False,
        **_kwargs: Any,
    ) -> None:
        answers.append((text, show_alert))

    async def handler(_event: Any, data: dict[str, Any]) -> str:
        handled_states.append(await data["state"].get_state())
        return "handled"

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    middleware = ClientPlatformInteractionSafetyMiddleware()
    state = _state()
    await state.set_state(control.ClientPlatformControlState.activity_description)
    callback = _callback("cpm:event-wizard:timezone:moscow")
    data = {"bot": type("Bot", (), {"id": 1})(), "state": state}

    assert await middleware(handler, callback, data) == "handled"
    assert handled_states == [None]
    assert ("Сначала завершите текущий шаг или отправьте /cancel.", True) not in answers


@pytest.mark.asyncio
async def test_repeatable_navigation_clears_stale_state_and_is_not_double_tap_blocked(
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

    async def edit_reply_markup(_message: Message, **_kwargs: Any) -> None:
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

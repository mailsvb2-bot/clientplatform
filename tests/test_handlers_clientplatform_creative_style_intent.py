from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from clientplatform.domain.visual_style_intent import VisualStyleIntent, visual_style_preset
from handlers import clientplatform_creative_studio as studio


class FakeState:
    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})
        self.state = None
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_data(self, data):
        self.data = dict(data)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.data.clear()
        self.state = None
        self.cleared = True


def _message(text: str = ""):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def _callback(data: str, target=None):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
        message=target or _message(),
    )


def _actor():
    return SimpleNamespace(
        business_id="business-id",
        assert_can_manage_promotions=Mock(),
    )


def _style_state(**overrides):
    value = {
        "creative_business_id": "business-id",
        "creative_business_token": "business-token",
        "creative_kind": "image",
        "creative_pending_prompt": (
            "колючий ёж слушает аудиосессию и превращается "
            "в доброго и мягкого"
        ),
        "creative_brand_context": "Brand name: Practice. Tone: calm.",
        "creative_country_code": "RU",
        "creative_style_intent": VisualStyleIntent().to_mapping(),
        "creative_saved_style_applied": False,
        "creative_style_inferred_fields": [],
    }
    value.update(overrides)
    return value


def test_short_prompt_enters_optional_style_step_before_any_paid_preparation(
    monkeypatch,
) -> None:
    message = _message(
        "колючий ёж слушает аудиосессию и превращается в доброго и мягкого"
    )
    state = FakeState(
        {
            "creative_business_id": "business-id",
            "creative_business_token": "business-token",
            "creative_kind": "image",
        }
    )
    prepare = Mock()
    monkeypatch.setattr(studio.control, "_user_id", lambda _message: 101)
    monkeypatch.setattr(studio.control, "_actor", AsyncMock(return_value=_actor()))
    monkeypatch.setattr(studio, "visual_generation_ready", lambda **_kwargs: True)
    monkeypatch.setattr(
        studio,
        "load_goal_visual_brand",
        lambda **_kwargs: SimpleNamespace(
            prompt_context=lambda: "Brand name: Practice. Tone: calm."
        ),
    )
    monkeypatch.setattr(
        studio,
        "load_visual_style_preference",
        lambda **_kwargs: visual_style_preset("premium"),
    )
    monkeypatch.setattr(studio, "prepare_creative_generation", prepare)

    asyncio.run(studio.receive_creative_prompt(message, state))

    prepare.assert_not_called()
    assert state.state == studio.ClientPlatformCreativeStudioState.choosing_style
    assert state.data["creative_pending_prompt"].startswith("колючий ёж")
    text = message.answer.await_args.args[0]
    assert "Технический промпт писать не нужно" in text
    buttons = [
        button.callback_data
        for row in message.answer.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "cpc:st:go:business-token" in buttons
    assert "cpc:sv:show:business-token" in buttons
    assert "cpc:st:open:business-token" in buttons
    labels = [
        button.text
        for row in message.answer.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "🤖 AI-авто — подготовить постановку" in labels
    assert "🎬 Подготовить 5 вариантов постановки" in labels
    assert "один текстовый AI-вызов" in text


def test_receive_prompt_rejects_corrupt_creative_kind_before_actor_lookup(
    monkeypatch,
) -> None:
    message = _message("обычная идея")
    state = FakeState(
        {
            "creative_business_id": "business-id",
            "creative_business_token": "business-token",
            "creative_kind": "corrupt-kind",
        }
    )
    actor = AsyncMock(return_value=_actor())
    monkeypatch.setattr(studio.control, "_user_id", lambda _message: 101)
    monkeypatch.setattr(studio.control, "_actor", actor)

    asyncio.run(studio.receive_creative_prompt(message, state))

    actor.assert_not_awaited()
    assert (
        "Опишите картинку или видео одним сообщением"
        in message.answer.await_args.args[0]
    )


def test_quick_style_callback_fails_closed_when_style_mapping_is_invalid(
    monkeypatch,
) -> None:
    target = _message()
    callback = _callback("cpc:st:p:wf:business-token", target)
    state = FakeState(_style_state())
    monkeypatch.setattr(studio, "style_preset_name", lambda _code: "unknown-style")

    asyncio.run(studio.choose_visual_style_preset(callback, state))

    assert callback.answer.await_args.args[0] == "Кнопка устарела"
    assert callback.answer.await_args.kwargs["show_alert"] is True
    target.answer.assert_not_awaited()


def test_quick_style_buttons_are_additive_multi_select_and_do_not_call_provider(
    monkeypatch,
) -> None:
    target = _message()
    state = FakeState(_style_state())
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    prepare = Mock()
    monkeypatch.setattr(studio, "prepare_creative_generation", prepare)

    for callback_data in (
        "cpc:st:p:wf:business-token",
        "cpc:st:p:il:business-token",
        "cpc:st:p:pr:business-token",
    ):
        asyncio.run(
            studio.choose_visual_style_preset(
                _callback(callback_data, target),
                state,
            )
        )

    prepare.assert_not_called()
    style = VisualStyleIntent.from_mapping(state.data["creative_style_intent"])
    assert style.quick_style_names() == (
        "warm_friendly",
        "premium",
        "illustrative",
    )
    labels = [
        button.text
        for row in target.answer.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "✅ 🤗 Тёпло и дружелюбно" in labels
    assert "✅ 💎 Премиально" in labels
    assert "✅ 🎨 Художественно" in labels
    assert "Акценты:" in target.answer.await_args.args[0]

    asyncio.run(
        studio.choose_visual_style_preset(
            _callback("cpc:st:p:pr:business-token", target),
            state,
        )
    )
    style = VisualStyleIntent.from_mapping(state.data["creative_style_intent"])
    assert style.quick_style_names() == ("warm_friendly", "illustrative")


def test_style_dashboard_stays_within_owner_button_budget() -> None:
    dashboard = studio.style_dashboard_rows("business-token", VisualStyleIntent())
    buttons = [button for row in dashboard for button in row]

    assert len(buttons) <= 20
    labels = [label for label, _callback in buttons]
    assert "🤖 Авто — всё решит ClientPlatform" in labels
    assert "✅ AI-авто — выбрать лучший" in labels


def test_style_rows_show_checkmark_for_current_choice_and_compact_finish_action() -> None:
    style = visual_style_preset("warm_friendly")
    dashboard = studio.style_dashboard_rows("business-token", style)
    dashboard_labels = [label for row in dashboard for label, _callback in row]
    assert "✅ 🤗 Тёпло и дружелюбно" in dashboard_labels
    assert "✅ AI-авто — выбрать лучший" in dashboard_labels

    mood_rows = studio.style_dimension_rows("business-token", "m", style)
    mood_labels = [label for row in mood_rows for label, _callback in row]
    assert "✅ 😊 Доброжелательная" in mood_labels
    assert "✅ AI-авто — выбрать лучший" in mood_labels
    assert "⬅️ Все настройки" in mood_labels


def test_style_dimension_updates_the_same_message_when_telegram_allows_edit(monkeypatch) -> None:
    target = _message()
    target.edit_text = AsyncMock()
    state = FakeState(_style_state())
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)

    open_dimension = _callback("cpc:st:d:m:business-token", target)
    asyncio.run(studio.open_visual_style_dimension(open_dimension, state))
    target.edit_text.assert_awaited_once()
    target.answer.assert_not_awaited()

    target.edit_text.reset_mock()
    selected = _callback("cpc:st:s:m:c:business-token", target)
    asyncio.run(studio.set_visual_style_dimension(selected, state))
    target.edit_text.assert_awaited_once()
    target.answer.assert_not_awaited()
    labels = [
        button.text
        for row in target.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "✅ 🧘 Спокойная" in labels


def test_style_dimension_can_override_preset_with_human_choice(monkeypatch) -> None:
    target = _message()
    state = FakeState(
        _style_state(
            creative_style_intent=visual_style_preset("cinematic").to_mapping()
        )
    )
    callback = _callback("cpc:st:s:t:w:business-token", target)
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)

    asyncio.run(studio.set_visual_style_dimension(callback, state))

    assert state.data["creative_style_intent"]["color_temperature"] == "warm"
    assert state.data["creative_style_intent"]["emotional_tone"] == "dramatic"


def test_save_style_is_explicit_and_does_not_start_generation(monkeypatch) -> None:
    target = _message()
    style = visual_style_preset("soft_calm")
    state = FakeState(_style_state(creative_style_intent=style.to_mapping()))
    callback = _callback("cpc:st:save:business-token", target)
    save = Mock(return_value=style)
    prepare = Mock()
    monkeypatch.setattr(studio.control, "_actor", AsyncMock(return_value=_actor()))
    monkeypatch.setattr(studio, "save_visual_style_preference", save)
    monkeypatch.setattr(studio, "prepare_creative_generation", prepare)

    asyncio.run(studio.save_current_visual_style(callback, state))

    save.assert_called_once()
    prepare.assert_not_called()
    assert state.data["creative_saved_style_applied"] is True
    assert "следующих визуалах" in callback.answer.await_args.args[0]


def test_paid_confirmation_replace_always_sends_fresh_keyboard_message(monkeypatch) -> None:
    target = _message()
    target.edit_reply_markup = AsyncMock()
    receipt = SimpleNamespace(
        id="receipt-id",
        request_text="ёж слушает аудиосессию",
        provider_payload_json='{"version":2}',
    )
    monkeypatch.setattr(studio, "_receipt_kind", lambda _receipt: "image")
    monkeypatch.setattr(
        studio,
        "_receipt_callback",
        lambda action, token, _receipt: f"receipt:{action}:{token}",
    )

    asyncio.run(
        studio._show_paid_generation_confirmation(
            target,
            token="business-token",
            receipt=receipt,
            replace=True,
        )
    )

    target.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    target.answer.assert_awaited_once()
    labels = [
        button.text
        for row in target.answer.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "✅ Создать 1 картинку" in labels
    assert "✏️ Изменить описание" in labels
    assert "⬅️ Не создавать" in labels


def test_paid_confirmation_survives_stale_keyboard_cleanup_error(monkeypatch) -> None:
    target = _message()
    target.edit_reply_markup = AsyncMock(
        side_effect=studio.TelegramAPIError(
            method=SimpleNamespace(),
            message="message is not modified",
        )
    )
    receipt = SimpleNamespace(
        id="receipt-id",
        request_text="ёж слушает аудиосессию",
        provider_payload_json='{"version":2}',
    )
    monkeypatch.setattr(studio, "_receipt_kind", lambda _receipt: "image")
    monkeypatch.setattr(
        studio,
        "_receipt_callback",
        lambda action, token, _receipt: f"receipt:{action}:{token}",
    )

    asyncio.run(
        studio._show_paid_generation_confirmation(
            target,
            token="business-token",
            receipt=receipt,
            replace=True,
        )
    )

    target.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    target.answer.assert_awaited_once()
    labels = [
        button.text
        for row in target.answer.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "✅ Создать 1 картинку" in labels


def test_continue_freezes_selected_style_before_paid_confirmation(monkeypatch) -> None:
    target = _message()
    style = visual_style_preset("warm_friendly")
    state = FakeState(_style_state(creative_style_intent=style.to_mapping()))
    captured = {}

    def freeze(**kwargs):
        captured.update(kwargs)
        return '{"version":2}'

    receipt = SimpleNamespace(
        id="receipt-id",
        request_text=state.data["creative_pending_prompt"],
        provider_payload_json='{"version":2}',
    )
    prepare = Mock(return_value=receipt)
    monkeypatch.setattr(studio.control, "_actor", AsyncMock(return_value=_actor()))
    monkeypatch.setattr(studio, "freeze_business_image_payload", freeze)
    monkeypatch.setattr(studio, "prepare_creative_generation", prepare)
    monkeypatch.setattr(studio, "_receipt_kind", lambda _receipt: "image")
    monkeypatch.setattr(
        studio,
        "_receipt_callback",
        lambda action, token, _receipt: f"receipt:{action}:{token}",
    )

    asyncio.run(
        studio._prepare_styled_generation(
            target,
            state,
            user_id=101,
            token="business-token",
        )
    )

    assert captured["style_intent"] == style
    prepare.assert_called_once()
    assert state.cleared is True
    assert "Платная генерация картинки/видео начнётся только после кнопки ниже" in target.answer.await_args.args[0]


def test_style_callbacks_are_state_local_in_creative_studio_safety(monkeypatch) -> None:
    from handlers import clientplatform_interaction_safety as safety

    monkeypatch.setattr(safety, "_creative_studio_safety_installed", False, raising=False)
    studio.install_creative_studio_safety(safety)

    assert safety._state_local_callback_allowed(
        "ClientPlatformCreativeStudioState:choosing_style",
        "cpc:st:open:business-token",
    )
    assert safety._state_local_callback_allowed(
        "ClientPlatformCreativeStudioState:choosing_style",
        "cpc:st:go:business-token",
    )


def test_style_state_helpers_cover_defaults_and_session_contract() -> None:
    assert studio._style_intent_from_state({}) == VisualStyleIntent()
    data = _style_state()
    assert studio._style_session_matches(data, "business-token")
    assert not studio._style_session_matches(data, "wrong-token")
    assert not studio._style_session_matches(
        _style_state(creative_business_id=""),
        "business-token",
    )
    assert not studio._style_session_matches(
        _style_state(creative_pending_prompt=""),
        "business-token",
    )
    assert not studio._style_session_matches(
        _style_state(creative_kind="other"),
        "business-token",
    )


def test_open_dimension_reset_and_confirm_style_callbacks(monkeypatch) -> None:
    target = _message()
    state = FakeState(_style_state())
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)

    opened = _callback("cpc:st:open:business-token", target)
    asyncio.run(studio.open_visual_style(opened, state))
    assert opened.answer.await_count == 1
    assert "Как Вы представляете" in target.answer.await_args.args[0]

    target.answer.reset_mock()
    dimension = _callback("cpc:st:d:t:business-token", target)
    asyncio.run(studio.open_visual_style_dimension(dimension, state))
    assert "Выберите вариант" in target.answer.await_args.args[0]

    target.answer.reset_mock()
    reset = _callback("cpc:st:reset:business-token", target)
    asyncio.run(studio.reset_visual_style(reset, state))
    assert reset.answer.await_args.args[0] == "Вернул автоматический стиль"
    assert target.answer.await_count == 1

    prepare = AsyncMock()
    monkeypatch.setattr(studio, "_prepare_styled_generation", prepare)
    confirm = _callback("cpc:st:go:business-token", target)
    asyncio.run(studio.confirm_visual_style(confirm, state))
    prepare.assert_awaited_once()
    assert prepare.await_args.kwargs["token"] == "business-token"


def test_style_callbacks_reject_malformed_and_stale_sessions() -> None:
    malformed_cases = (
        (studio.choose_visual_style_preset, "cpc:st:p:bad"),
        (studio.open_visual_style_dimension, "cpc:st:d:bad"),
        (studio.set_visual_style_dimension, "cpc:st:s:bad"),
    )
    for handler, data in malformed_cases:
        callback = _callback(data)
        asyncio.run(handler(callback, FakeState(_style_state())))
        assert callback.answer.await_args.kwargs["show_alert"] is True

    stale = FakeState(_style_state(creative_business_token="other-token"))
    stale_cases = (
        (studio.open_visual_style, "cpc:st:open:business-token"),
        (studio.choose_visual_style_preset, "cpc:st:p:sc:business-token"),
        (studio.open_visual_style_dimension, "cpc:st:d:t:business-token"),
        (studio.set_visual_style_dimension, "cpc:st:s:t:w:business-token"),
        (studio.reset_visual_style, "cpc:st:reset:business-token"),
        (studio.save_current_visual_style, "cpc:st:save:business-token"),
        (studio.clear_current_visual_style, "cpc:st:clear:business-token"),
        (studio.confirm_visual_style, "cpc:st:go:business-token"),
    )
    for handler, data in stale_cases:
        callback = _callback(data)
        asyncio.run(handler(callback, stale))
        assert callback.answer.await_args.kwargs["show_alert"] is True
        assert "устарела" in callback.answer.await_args.args[0]


def test_clear_style_and_style_persistence_error_paths(monkeypatch) -> None:
    style = visual_style_preset("warm_friendly")
    state = FakeState(
        _style_state(
            creative_style_intent=style.to_mapping(),
            creative_saved_style_applied=True,
        )
    )
    callback = _callback("cpc:st:clear:business-token")
    clear = Mock(return_value=True)
    monkeypatch.setattr(studio.control, "_actor", AsyncMock(return_value=_actor()))
    monkeypatch.setattr(studio, "clear_visual_style_preference", clear)

    asyncio.run(studio.clear_current_visual_style(callback, state))

    clear.assert_called_once()
    assert state.data["creative_saved_style_applied"] is False
    assert callback.answer.await_args.args[0] == "Сохранённый стиль сброшен"

    denied = studio.TenantPermissionDenied("denied")
    monkeypatch.setattr(studio.control, "_actor", AsyncMock(side_effect=denied))
    save_denied = _callback("cpc:st:save:business-token")
    asyncio.run(studio.save_current_visual_style(save_denied, FakeState(_style_state())))
    assert save_denied.answer.await_args.kwargs["show_alert"] is True
    assert "недоступна" in save_denied.answer.await_args.args[0]

    clear_denied = _callback("cpc:st:clear:business-token")
    asyncio.run(studio.clear_current_visual_style(clear_denied, FakeState(_style_state())))
    assert clear_denied.answer.await_args.kwargs["show_alert"] is True

    monkeypatch.setattr(studio.control, "_actor", AsyncMock(return_value=_actor()))
    monkeypatch.setattr(
        studio,
        "_style_intent_from_state",
        Mock(side_effect=ValueError("bad style")),
    )
    save_bad = _callback("cpc:st:save:business-token")
    asyncio.run(studio.save_current_visual_style(save_bad, FakeState(_style_state())))
    assert "Не удалось запомнить стиль" in save_bad.answer.await_args.args[0]

    monkeypatch.setattr(studio, "_style_intent_from_state", lambda data: VisualStyleIntent())
    monkeypatch.setattr(
        studio,
        "clear_visual_style_preference",
        Mock(side_effect=ValueError("bad clear")),
    )
    clear_bad = _callback("cpc:st:clear:business-token")
    asyncio.run(studio.clear_current_visual_style(clear_bad, FakeState(_style_state())))
    assert "Не удалось сбросить" in clear_bad.answer.await_args.args[0]


def test_prepare_styled_generation_rejects_stale_permission_and_invalid_payload(
    monkeypatch,
) -> None:
    target = _message()
    asyncio.run(
        studio._prepare_styled_generation(
            target,
            FakeState(_style_state(creative_business_token="other")),
            user_id=101,
            token="business-token",
        )
    )
    assert "устарела" in target.answer.await_args.args[0]

    target.answer.reset_mock()
    monkeypatch.setattr(
        studio.control,
        "_actor",
        AsyncMock(side_effect=studio.TenantPermissionDenied("denied")),
    )
    asyncio.run(
        studio._prepare_styled_generation(
            target,
            FakeState(_style_state()),
            user_id=101,
            token="business-token",
        )
    )
    assert "недоступно" in target.answer.await_args.args[0]

    target.answer.reset_mock()
    monkeypatch.setattr(studio.control, "_actor", AsyncMock(return_value=_actor()))
    monkeypatch.setattr(
        studio,
        "freeze_business_image_payload",
        Mock(side_effect=ValueError("invalid frozen payload")),
    )
    asyncio.run(
        studio._prepare_styled_generation(
            target,
            FakeState(_style_state()),
            user_id=101,
            token="business-token",
        )
    )
    assert "безопасно подготовить" in target.answer.await_args.args[0]


def test_paid_video_confirmation_exposes_video_specific_copy(monkeypatch) -> None:
    target = _message()
    receipt = SimpleNamespace(
        id="receipt-id",
        request_text="анимация спокойной сцены",
        provider_payload_json='{"version":2}',
    )
    monkeypatch.setattr(studio, "_receipt_kind", lambda _receipt: "video")
    monkeypatch.setattr(
        studio,
        "_receipt_callback",
        lambda action, token, _receipt: f"receipt:{action}:{token}",
    )

    asyncio.run(
        studio._show_paid_generation_confirmation(
            target,
            token="business-token",
            receipt=receipt,
        )
    )

    text = target.answer.await_args.args[0]
    assert "полноценный генератор движущейся сцены" in text
    labels = [
        button.text
        for row in target.answer.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    callbacks = [
        button.callback_data
        for row in target.answer.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "✅ Создать 1 видео" in labels
    assert "cpc:video:business-token" in callbacks


def test_accept_result_contract_success_and_errors(monkeypatch) -> None:
    invalid = _callback("cpc:accept:broken")
    asyncio.run(studio.accept_creative_result(invalid, FakeState()))
    assert "устарела" in invalid.answer.await_args.args[0]

    monkeypatch.setattr(
        studio,
        "_actor_for_callback",
        AsyncMock(side_effect=studio.TenantPermissionDenied("denied")),
    )
    denied = _callback("cpc:accept:business-token:receipt-token")
    asyncio.run(studio.accept_creative_result(denied, FakeState()))
    assert "недоступен" in denied.answer.await_args.args[0]

    actor = _actor()
    monkeypatch.setattr(studio, "_actor_for_callback", AsyncMock(return_value=actor))
    monkeypatch.setattr(
        studio,
        "_receipt_for_callback",
        AsyncMock(side_effect=LookupError("gone")),
    )
    missing = _callback("cpc:accept:business-token:receipt-token")
    asyncio.run(studio.accept_creative_result(missing, FakeState()))
    assert "уже недоступен" in missing.answer.await_args.args[0]

    monkeypatch.setattr(
        studio,
        "_receipt_for_callback",
        AsyncMock(return_value=SimpleNamespace(id="receipt-id")),
    )
    accepted = _callback("cpc:accept:business-token:receipt-token")
    asyncio.run(studio.accept_creative_result(accepted, FakeState()))
    assert accepted.answer.await_args.args[0] == "Хорошо — оставляем этот вариант"


def test_restyle_result_reopens_same_idea_and_covers_errors(monkeypatch) -> None:
    invalid = _callback("cpc:restyle:broken")
    asyncio.run(studio.restyle_creative_result(invalid, FakeState()))
    assert "устарела" in invalid.answer.await_args.args[0]

    monkeypatch.setattr(
        studio,
        "_actor_for_callback",
        AsyncMock(side_effect=studio.TenantPermissionDenied("denied")),
    )
    denied = _callback("cpc:restyle:business-token:receipt-token")
    asyncio.run(studio.restyle_creative_result(denied, FakeState()))
    assert "недоступен" in denied.answer.await_args.args[0]

    actor = _actor()
    receipt = SimpleNamespace(
        id="receipt-id",
        request_text="тёплая спокойная сцена",
        brand_context="Brand name: Practice.",
        country_code="RU",
        provider_payload_json='{"version":2}',
    )
    target = _message()
    state = FakeState()
    monkeypatch.setattr(studio, "_actor_for_callback", AsyncMock(return_value=actor))
    monkeypatch.setattr(studio, "_receipt_for_callback", AsyncMock(return_value=receipt))
    monkeypatch.setattr(
        studio,
        "frozen_business_visual_style",
        lambda _payload: visual_style_preset("warm_friendly"),
    )
    monkeypatch.setattr(studio, "_receipt_kind", lambda _receipt: "video")
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)

    callback = _callback("cpc:restyle:business-token:receipt-token", target)
    asyncio.run(studio.restyle_creative_result(callback, state))

    assert state.state == studio.ClientPlatformCreativeStudioState.choosing_style
    assert state.data["creative_kind"] == "video"
    assert state.data["creative_pending_prompt"] == receipt.request_text
    assert callback.answer.await_args.args[0] == "Меняем только визуальный стиль"
    assert target.answer.await_count == 1

    monkeypatch.setattr(
        studio,
        "_receipt_for_callback",
        AsyncMock(side_effect=ValueError("broken frozen receipt")),
    )
    broken = _callback("cpc:restyle:business-token:receipt-token")
    asyncio.run(studio.restyle_creative_result(broken, FakeState()))
    assert "уже недоступен" in broken.answer.await_args.args[0]


def test_variant_result_reuses_frozen_payload_and_covers_errors(monkeypatch) -> None:
    invalid = _callback("cpc:variant:broken")
    asyncio.run(studio.create_another_visual_variant(invalid, FakeState()))
    assert "устарела" in invalid.answer.await_args.args[0]

    monkeypatch.setattr(
        studio,
        "_actor_for_callback",
        AsyncMock(side_effect=studio.TenantPermissionDenied("denied")),
    )
    denied = _callback("cpc:variant:business-token:receipt-token")
    asyncio.run(studio.create_another_visual_variant(denied, FakeState()))
    assert "недоступно" in denied.answer.await_args.args[0]

    actor = _actor()
    receipt = SimpleNamespace(
        id="receipt-id",
        request_text="ёж слушает аудиосессию",
        brand_context="Brand name: Practice.",
        country_code="RU",
        provider_payload_json='{"version":2,"brief":{"kind":"image"}}',
    )
    new_receipt = SimpleNamespace(id="new-receipt")
    prepare = Mock(return_value=new_receipt)
    confirmation = AsyncMock()
    target = _message()
    monkeypatch.setattr(studio, "_actor_for_callback", AsyncMock(return_value=actor))
    monkeypatch.setattr(studio, "_receipt_for_callback", AsyncMock(return_value=receipt))
    monkeypatch.setattr(studio, "prepare_creative_generation", prepare)
    monkeypatch.setattr(studio, "_show_paid_generation_confirmation", confirmation)
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)

    callback = _callback("cpc:variant:business-token:receipt-token", target)
    asyncio.run(studio.create_another_visual_variant(callback, FakeState()))

    assert prepare.call_args.kwargs["provider_payload_json"] == receipt.provider_payload_json
    assert "отдельная платная генерация" in target.answer.await_args.args[0]
    confirmation.assert_awaited_once_with(
        target,
        token="business-token",
        receipt=new_receipt,
    )

    monkeypatch.setattr(
        studio,
        "_receipt_for_callback",
        AsyncMock(side_effect=LookupError("gone")),
    )
    missing = _callback("cpc:variant:business-token:receipt-token")
    asyncio.run(studio.create_another_visual_variant(missing, FakeState()))
    assert "Не удалось подготовить другой вариант" in missing.answer.await_args.args[0]


def test_creative_safety_delegates_outside_style_state_and_is_idempotent() -> None:
    safety = SimpleNamespace(
        _creative_studio_safety_installed=False,
        _CLIENTPLATFORM_CALLBACK_PREFIXES=("base:",),
        _STATE_ESCAPE_PREFIXES=("base:",),
        _REPEATABLE_NAVIGATION_PREFIXES=("base:",),
        _ONE_SHOT_PREFIXES=("base:",),
        _state_local_callback_allowed=lambda state, data: data == "orig-local",
        _callback_can_escape_state=lambda state, data: data == "orig-escape",
    )

    studio.install_creative_studio_safety(safety)

    assert safety._state_local_callback_allowed(
        "ClientPlatformCreativeStudioState:choosing_style",
        "cpc:new:business-token",
    )
    assert safety._state_local_callback_allowed("OtherState:x", "orig-local")
    assert not safety._state_local_callback_allowed("OtherState:x", "other")
    assert safety._callback_can_escape_state(
        "ClientPlatformCreativeStudioState:choosing_style",
        "cpc:open:business-token",
    )
    assert safety._callback_can_escape_state("OtherState:x", "orig-escape")
    assert not safety._callback_can_escape_state("OtherState:x", "other")

    prefixes = (
        safety._CLIENTPLATFORM_CALLBACK_PREFIXES,
        safety._STATE_ESCAPE_PREFIXES,
        safety._REPEATABLE_NAVIGATION_PREFIXES,
        safety._ONE_SHOT_PREFIXES,
    )
    studio.install_creative_studio_safety(safety)
    assert prefixes == (
        safety._CLIENTPLATFORM_CALLBACK_PREFIXES,
        safety._STATE_ESCAPE_PREFIXES,
        safety._REPEATABLE_NAVIGATION_PREFIXES,
        safety._ONE_SHOT_PREFIXES,
    )


if __name__ == "__main__":
    raise SystemExit("run with pytest")


def test_visual_language_choice_overrides_conflicting_owner_style_wording(monkeypatch) -> None:
    target = _message()
    state = FakeState(
        _style_state(
            creative_pending_prompt="городская улица вечером в стиле акварели"
        )
    )
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)

    callback = _callback("cpc:st:s:r:p:business-token", target)
    asyncio.run(studio.set_visual_style_dimension(callback, state))

    assert state.data["creative_style_intent"]["realism"] == "photorealistic"
    assert state.data["creative_override_owner_style_wording"] is True


def test_non_medium_style_choice_keeps_owner_authored_medium_authoritative(monkeypatch) -> None:
    target = _message()
    state = FakeState(
        _style_state(
            creative_pending_prompt="городская улица вечером в стиле акварели"
        )
    )
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)

    callback = _callback("cpc:st:s:t:w:business-token", target)
    asyncio.run(studio.set_visual_style_dimension(callback, state))

    assert state.data["creative_style_intent"]["color_temperature"] == "warm"
    assert state.data["creative_override_owner_style_wording"] is False


def test_photo_quick_style_overrides_conflicting_owner_medium(monkeypatch) -> None:
    target = _message()
    state = FakeState(
        _style_state(
            creative_pending_prompt="городская улица вечером в стиле акварели"
        )
    )
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)

    callback = _callback("cpc:st:p:np:business-token", target)
    asyncio.run(studio.choose_visual_style_preset(callback, state))

    assert state.data["creative_override_owner_style_wording"] is True


def test_style_reset_restores_owner_authored_medium_precedence(monkeypatch) -> None:
    target = _message()
    state = FakeState(
        _style_state(
            creative_pending_prompt="городская улица вечером в стиле акварели",
            creative_override_owner_style_wording=True,
        )
    )
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)

    callback = _callback("cpc:st:reset:business-token", target)
    asyncio.run(studio.reset_visual_style(callback, state))

    assert state.data["creative_override_owner_style_wording"] is False

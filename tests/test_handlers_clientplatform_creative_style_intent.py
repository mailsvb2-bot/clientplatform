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
        "creative_saved_style": VisualStyleIntent().to_mapping(),
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
    assert "cpc:st:open:business-token" in buttons


def test_style_preset_changes_only_preparation_state_not_provider(monkeypatch) -> None:
    target = _message()
    callback = _callback("cpc:st:p:sc:business-token", target)
    state = FakeState(_style_state())
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    prepare = Mock()
    monkeypatch.setattr(studio, "prepare_creative_generation", prepare)

    asyncio.run(studio.choose_visual_style_preset(callback, state))

    prepare.assert_not_called()
    assert state.data["creative_style_intent"]["emotional_tone"] == "calm"
    assert state.data["creative_style_intent"]["contrast"] == "soft"
    assert "Как Вы представляете" in target.answer.await_args.args[0]


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
    assert "Платный вызов начнётся только после кнопки ниже" in target.answer.await_args.args[0]


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


if __name__ == "__main__":
    raise SystemExit("run with pytest")

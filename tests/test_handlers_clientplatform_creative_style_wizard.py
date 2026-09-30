from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from clientplatform.domain.visual_style_intent import VisualStyleIntent
from handlers import clientplatform_creative_studio as studio


class FakeState:
    def __init__(self, data: dict | None = None):
        self.data = dict(data or {})
        self.state = None
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def set_state(self, state):
        self.state = state

    async def set_data(self, data):
        self.data = dict(data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def clear(self):
        self.cleared = True
        self.data = {}
        self.state = None


def _buttons(markup):
    return [
        (button.text, button.callback_data)
        for row in markup.inline_keyboard
        for button in row
    ]


def test_style_entry_exposes_fast_and_optional_paths() -> None:
    rows = _buttons(studio._style_entry_rows("business-token", has_saved=True))

    assert ("✨ Создать сразу", "cpc:stgo:business-token") in rows
    assert ("🎨 Уточнить стиль", "cpc:stopen:business-token") in rows
    assert ("⭐ Мой обычный стиль", "cpc:stsaved:business-token") in rows
    assert ("✏️ Изменить идею", "cpc:strewrite:business-token") in rows


def test_style_presets_include_human_one_tap_choices() -> None:
    rows = _buttons(studio._preset_rows("business-token"))
    labels = {label for label, _callback in rows}

    assert "🤗 Тёпло и дружелюбно" in labels
    assert "🧘 Мягко и спокойно" in labels
    assert "🏢 Чисто и профессионально" in labels
    assert "💎 Премиально" in labels
    assert "🎬 Кинематографично" in labels
    assert "🎨 Сказочно" in labels
    assert "⚙️ Настроить вручную" in labels


def test_receiving_short_prompt_does_not_start_or_prepare_paid_generation(
    monkeypatch,
) -> None:
    actor = SimpleNamespace(
        business_id="business-id",
        assert_can_manage_promotions=Mock(),
    )
    state = FakeState(
        {
            "creative_business_id": "business-id",
            "creative_business_token": "business-token",
            "creative_kind": "image",
        }
    )
    message = SimpleNamespace(
        text=(
            "колючий ёж, который слушает метротерапию "
            "и превращается в доброго и мягкого"
        ),
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )
    prepare = Mock()

    monkeypatch.setattr(studio.control, "_user_id", lambda _message: 101)
    monkeypatch.setattr(studio.control, "_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(
        studio,
        "load_visual_style_preference",
        lambda **_kwargs: VisualStyleIntent(),
    )
    monkeypatch.setattr(studio, "prepare_creative_generation", prepare)

    asyncio.run(studio.receive_creative_prompt(message, state))

    prepare.assert_not_called()
    assert state.state == studio.ClientPlatformCreativeStudioState.choosing_style
    assert state.data["creative_prompt"].startswith("колючий ёж")
    text = message.answer.await_args.args[0]
    assert "Технический промпт ClientPlatform составит сам" in text
    assert "тёп" not in text.casefold()
    rows = _buttons(message.answer.await_args.kwargs["reply_markup"])
    assert ("✨ Создать сразу", "cpc:stgo:business-token") in rows
    assert ("🎨 Уточнить стиль", "cpc:stopen:business-token") in rows


def test_prepare_generation_freezes_resolved_style_before_paid_consent(
    monkeypatch,
) -> None:
    actor = SimpleNamespace(business_id="business-id")
    state = FakeState({"creative_kind": "image"})
    target = SimpleNamespace(answer=AsyncMock())
    frozen = Mock(return_value='{"version":2}')
    prepared = SimpleNamespace(
        request_text="тёплая фотография кабинета",
        provider_payload_json='{"version":2}',
    )

    async def immediate(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(studio.asyncio, "to_thread", immediate)
    monkeypatch.setattr(studio, "visual_generation_ready", lambda **_kwargs: True)
    monkeypatch.setattr(
        studio,
        "load_goal_visual_brand",
        lambda **_kwargs: SimpleNamespace(
            prompt_context=lambda: "Brand name: Practice. Tone: calm."
        ),
    )
    monkeypatch.setattr(studio, "freeze_business_image_payload", frozen)
    monkeypatch.setattr(
        studio,
        "prepare_creative_generation",
        lambda **_kwargs: prepared,
    )
    monkeypatch.setattr(studio, "_receipt_kind", lambda _receipt: "image")

    asyncio.run(
        studio._prepare_creative_with_style(
            target,
            state,
            actor=actor,
            token="business-token",
            request="тёплая фотография кабинета",
            saved=VisualStyleIntent(realism="illustrative"),
            explicit=VisualStyleIntent(
                color_temperature="cool",
                emotional_tone="dramatic",
            ),
            remember_style=False,
        )
    )

    style = frozen.call_args.kwargs["style_intent"]
    assert style.color_temperature == "cool"
    assert style.emotional_tone == "dramatic"
    assert style.realism == "realistic"
    assert state.cleared is True
    assert "Смысл запроса и выбранный стиль уже зафиксированы" in (
        target.answer.await_args.args[0]
    )


def test_create_and_remember_saves_resolved_style_only_after_freeze(
    monkeypatch,
) -> None:
    actor = SimpleNamespace(business_id="business-id")
    state = FakeState({"creative_kind": "image"})
    target = SimpleNamespace(answer=AsyncMock())
    events: list[str] = []

    async def immediate(function, *args, **kwargs):
        return function(*args, **kwargs)

    def freeze(**kwargs):
        events.append("freeze")
        assert kwargs["style_intent"].emotional_tone == "friendly"
        return '{"version":2}'

    def prepare(**_kwargs):
        events.append("prepare")
        return SimpleNamespace(
            request_text="уютный кабинет",
            provider_payload_json='{"version":2}',
        )

    def save(**kwargs):
        events.append("save")
        return kwargs["style"]

    monkeypatch.setattr(studio.asyncio, "to_thread", immediate)
    monkeypatch.setattr(studio, "visual_generation_ready", lambda **_kwargs: True)
    monkeypatch.setattr(
        studio,
        "load_goal_visual_brand",
        lambda **_kwargs: SimpleNamespace(prompt_context=lambda: "Brand"),
    )
    monkeypatch.setattr(studio, "freeze_business_image_payload", freeze)
    monkeypatch.setattr(studio, "prepare_creative_generation", prepare)
    monkeypatch.setattr(studio, "save_visual_style_preference", save)
    monkeypatch.setattr(studio, "_receipt_kind", lambda _receipt: "image")

    asyncio.run(
        studio._prepare_creative_with_style(
            target,
            state,
            actor=actor,
            token="business-token",
            request="уютный кабинет",
            saved=VisualStyleIntent(),
            explicit=VisualStyleIntent(emotional_tone="friendly"),
            remember_style=True,
        )
    )

    assert events == ["freeze", "prepare", "save"]

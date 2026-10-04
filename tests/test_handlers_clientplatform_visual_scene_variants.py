from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from clientplatform.application.visual_scene_variants import (
    build_visual_scene_variants,
)
from clientplatform.domain.visual_prompt_compiler import semantic_flags_for_request
from clientplatform.domain.visual_scene_contract import fallback_scene_contract
from clientplatform.domain.visual_style_intent import VisualStyleIntent
from handlers import clientplatform_creative_studio as studio


class FakeState:
    def __init__(self, data: dict) -> None:
        self.data = dict(data)
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.data.clear()
        self.state = None


def _data() -> dict:
    request = "ёж, который слушает ресурсное аудио и становится добрым и пушистым"
    flags = semantic_flags_for_request(request)
    contract = fallback_scene_contract(request=request, semantic_flags=flags)
    variants = build_visual_scene_variants(
        request=request,
        scene_contract=contract,
        style_intent=VisualStyleIntent(),
        client=None,
    )
    return {
        "creative_business_id": "business-id",
        "creative_business_token": "business-token",
        "creative_kind": "image",
        "creative_pending_prompt": request,
        "creative_brand_context": "",
        "creative_country_code": "RU",
        "creative_style_intent": VisualStyleIntent().to_mapping(),
        "creative_scene_contract": contract.to_mapping(),
        "creative_scene_planner_source": "deterministic",
        "creative_scene_variants": [item.to_mapping() for item in variants],
    }


def _target(text: str = ""):
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
        message=target or _target(),
    )


def test_each_scene_variant_has_choose_and_supplement_buttons(monkeypatch) -> None:
    monkeypatch.setenv("VISUAL_SCENE_VARIANTS_ENABLED", "0")
    data = _data()
    variants = studio._scene_variants_from_state(data)

    rows = studio._scene_variant_rows("business-token", variants)
    callbacks = [callback for row in rows for _label, callback in row]
    labels = [label for row in rows for label, _callback in row]

    assert len([item for item in labels if item == "✍️ Дополнить своим"]) == 5
    for index in range(1, 6):
        assert f"cpc:sv:pick:v{index}:business-token" in callbacks
        assert f"cpc:sv:add:v{index}:business-token" in callbacks
    assert "cpc:sv:auto:business-token" in callbacks


def test_supplement_flow_updates_only_selected_variant(monkeypatch) -> None:
    monkeypatch.setenv("VISUAL_SCENE_VARIANTS_ENABLED", "0")
    state = FakeState(_data())
    target = _target()
    callback = _callback("cpc:sv:add:v2:business-token", target)
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)

    asyncio.run(studio.ask_scene_variant_supplement(callback, state))

    assert state.state == studio.ClientPlatformCreativeStudioState.waiting_scene_supplement
    assert state.data["creative_scene_selected_variant_id"] == "v2"
    assert "✍️ Дополнить своим" in target.answer.await_args.args[0]

    message = _target("ночной мягкий свет, камера немного ниже")
    before = studio._scene_variants_from_state(state.data)
    asyncio.run(studio.receive_scene_variant_supplement(message, state))
    after = studio._scene_variants_from_state(state.data)

    assert state.state == studio.ClientPlatformCreativeStudioState.choosing_style
    assert before[0] == after[0]
    assert before[2:] == after[2:]
    assert after[1].user_supplement == "ночной мягкий свет, камера немного ниже"
    assert "canonical semantic contract" in after[1].direction
    labels = [
        button.text
        for row in message.answer.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "✅ Использовать этот вариант" in labels
    assert "✍️ Дополнить своим ещё" in labels


def test_pick_scene_variant_passes_exact_selected_variant_to_freezer(monkeypatch) -> None:
    monkeypatch.setenv("VISUAL_SCENE_VARIANTS_ENABLED", "0")
    state = FakeState(_data())
    target = _target()
    callback = _callback("cpc:sv:pick:v4:business-token", target)
    prepared = AsyncMock()
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(studio, "_prepare_styled_generation", prepared)

    asyncio.run(studio.pick_scene_variant(callback, state))

    prepared.assert_awaited_once()
    selected = prepared.await_args.kwargs["scene_variant"]
    assert selected.id == "v4"
    assert prepared.await_args.kwargs["token"] == "business-token"


def test_auto_scene_variant_uses_recommended_variant(monkeypatch) -> None:
    monkeypatch.setenv("VISUAL_SCENE_VARIANTS_ENABLED", "0")
    state = FakeState(_data())
    target = _target()
    callback = _callback("cpc:sv:auto:business-token", target)
    prepared = AsyncMock()
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(
        studio.control,
        "_actor",
        AsyncMock(return_value=SimpleNamespace(assert_can_manage_promotions=lambda: None)),
    )
    monkeypatch.setattr(studio, "_prepare_styled_generation", prepared)

    asyncio.run(studio.auto_scene_variant(callback, state))

    prepared.assert_awaited_once()
    selected = prepared.await_args.kwargs["scene_variant"]
    assert selected.id in {"v1", "v5"}
    assert callback.answer.await_args.args[0] == "Выбран лучший вариант"

def test_scene_variant_helpers_roundtrip_cached_state_and_reject_corruption(monkeypatch) -> None:
    monkeypatch.setenv("VISUAL_SCENE_VARIANTS_ENABLED", "0")
    data = _data()
    state = FakeState(data)

    variants = studio._scene_variants_from_state(data)
    text = studio._scene_variant_text(variants)
    assert "Варианты постановки" in text
    assert "1. Ясная сюжетная сцена" in text
    assert "5." in text

    contract, source, cached = asyncio.run(
        studio._ensure_scene_variants(
            state,
            data,
            actor=SimpleNamespace(),
        )
    )
    assert source == "deterministic"
    assert contract == studio._scene_contract_from_state(data)
    assert cached == variants
    assert studio._variant_by_id(variants, "v3") == variants[2]

    broken = dict(data)
    broken["creative_scene_variants"] = "broken"
    try:
        studio._scene_variants_from_state(broken)
    except ValueError as exc:
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("corrupt variant state must fail closed")

    shortened = dict(data)
    shortened["creative_scene_variants"] = shortened["creative_scene_variants"][:4]
    try:
        studio._scene_variants_from_state(shortened)
    except ValueError as exc:
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("incomplete variant state must fail closed")

    try:
        studio._variant_by_id(variants, "v9")
    except ValueError as exc:
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("unknown variant id must fail closed")


def test_show_scene_variant_choices_handles_stale_success_and_planner_failure(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VISUAL_SCENE_VARIANTS_ENABLED", "0")

    monkeypatch.setattr(
        studio.control,
        "_actor",
        AsyncMock(return_value=SimpleNamespace(assert_can_manage_promotions=lambda: None)),
    )

    stale_state = FakeState(_data())
    stale_state.data["creative_business_token"] = "other-token"
    stale_target = _target()
    asyncio.run(
        studio._show_scene_variant_choices(
            stale_target,
            stale_state,
            token="business-token",
            user_id=101,
        )
    )
    assert "устарела" in stale_target.answer.await_args.args[0]

    state = FakeState(_data())
    target = _target()
    asyncio.run(
        studio._show_scene_variant_choices(
            target,
            state,
            token="business-token",
        )
    )
    assert state.state == studio.ClientPlatformCreativeStudioState.choosing_style
    assert "Варианты постановки" in target.answer.await_args.args[0]
    labels = [
        button.text
        for row in target.answer.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert labels.count("✍️ Дополнить своим") == 5
    assert "🤖 Выбрать лучший автоматически" in labels

    failing_state = FakeState(_data())
    failing_target = _target()
    monkeypatch.setattr(
        studio,
        "_ensure_scene_variants",
        AsyncMock(side_effect=OSError("planner unavailable")),
    )
    asyncio.run(
        studio._show_scene_variant_choices(
            failing_target,
            failing_state,
            token="business-token",
        )
    )
    assert "Не удалось подготовить варианты" in failing_target.answer.await_args.args[0]


def test_scene_variant_callbacks_fail_closed_for_stale_and_malformed_state(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VISUAL_SCENE_VARIANTS_ENABLED", "0")
    target = _target()

    malformed_pick = _callback("cpc:sv:pick:broken", target)
    asyncio.run(studio.pick_scene_variant(malformed_pick, FakeState(_data())))
    malformed_pick.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

    malformed_add = _callback("cpc:sv:add:broken", target)
    asyncio.run(studio.ask_scene_variant_supplement(malformed_add, FakeState(_data())))
    malformed_add.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

    stale_data = _data()
    stale_data["creative_business_token"] = "other-token"
    stale_auto = _callback("cpc:sv:auto:business-token", target)
    asyncio.run(studio.auto_scene_variant(stale_auto, FakeState(stale_data)))
    stale_auto.answer.assert_awaited_once_with(
        "Эта настройка уже устарела",
        show_alert=True,
    )

    broken_data = _data()
    broken_data["creative_scene_variants"] = []
    broken_pick = _callback("cpc:sv:pick:v1:business-token", target)
    asyncio.run(studio.pick_scene_variant(broken_pick, FakeState(broken_data)))
    broken_pick.answer.assert_awaited_once_with("Варианты устарели", show_alert=True)


def test_scene_variant_supplement_missing_selection_keeps_user_in_recovery_flow(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VISUAL_SCENE_VARIANTS_ENABLED", "0")
    state = FakeState(_data())
    message = _target("добавить мягкий вечерний свет")

    asyncio.run(studio.receive_scene_variant_supplement(message, state))

    assert "Дополнение не удалось сохранить" in message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].text == "⬅️ К вариантам"


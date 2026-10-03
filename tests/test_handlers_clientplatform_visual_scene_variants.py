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
    monkeypatch.setattr(studio, "_prepare_styled_generation", prepared)

    asyncio.run(studio.auto_scene_variant(callback, state))

    prepared.assert_awaited_once()
    selected = prepared.await_args.kwargs["scene_variant"]
    assert selected.id in {"v1", "v5"}
    assert callback.answer.await_args.args[0] == "Выбран лучший вариант"

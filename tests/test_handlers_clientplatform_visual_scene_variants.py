from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from clientplatform.application.visual_scene_variants import (
    build_visual_scene_bundle,
    build_visual_scene_variants,
    freeze_visual_scene_bundle,
)
from clientplatform.domain.visual_prompt_compiler import semantic_flags_for_request
from clientplatform.domain.visual_scene_contract import fallback_scene_contract
from clientplatform.domain.visual_scene_plan import VisualScenePlanStatus
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


def test_deterministic_variants_explain_distinct_semantic_staging(monkeypatch) -> None:
    monkeypatch.setenv("VISUAL_SCENE_VARIANTS_ENABLED", "0")
    data = _data()
    variants = studio._scene_variants_from_state(data)

    assert [item.title for item in variants] == [
        "Причина и результат в одном кадре",
        "История через ключевой момент",
        "Действие и изменение вместе",
        "Крупный фокус на изменении",
        "Единый кадр с визуальным потоком",
    ]
    descriptions = " ".join(item.description for item in variants)
    directions = " ".join(item.direction for item in variants)
    assert "ёж" in descriptions
    assert "слушает ресурсное аудио" in descriptions
    assert "ёж, который слушает ресурсное аудио" in directions
    assert "герой не дублируется" in descriptions
    assert "Три связанных этапа" not in descriptions
    assert len({item.description for item in variants}) == 5


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
    assert selected.id == "v1"
    assert callback.answer.await_args.args[0] == "Выбран лучший вариант"

def test_scene_variant_helpers_roundtrip_cached_state_and_reject_corruption(monkeypatch) -> None:
    monkeypatch.setenv("VISUAL_SCENE_VARIANTS_ENABLED", "0")
    data = _data()
    state = FakeState(data)

    variants = studio._scene_variants_from_state(data)
    text = studio._scene_variant_text(variants)
    assert "Варианты постановки" in text
    assert "1. Причина и результат в одном кадре" in text
    assert "ёж" in text
    assert "слушает ресурсное аудио" in text
    assert "AI-режиссёр сейчас недоступен" in text
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
            user_id=101,
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
            user_id=101,
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


def test_durable_ready_scene_plan_is_reused_without_second_ai_call(monkeypatch) -> None:
    monkeypatch.setattr(studio, "visual_scene_ai_planning_available", lambda: True)
    data = _data()
    contract = studio._scene_contract_from_state(data)
    variants = studio._scene_variants_from_state(data)
    frozen = freeze_visual_scene_bundle(
        scene_contract=contract,
        planner_source="ai",
        variants=variants,
    )
    for key in (
        "creative_scene_contract",
        "creative_scene_planner_source",
        "creative_scene_variants",
    ):
        data.pop(key, None)
    state = FakeState(data)
    actor = SimpleNamespace()
    receipt = SimpleNamespace(
        id="11111111-1111-4111-8111-111111111111",
        status=VisualScenePlanStatus.READY,
        result_json=frozen,
    )
    build = Mock(side_effect=AssertionError("paid planner must not run twice"))
    monkeypatch.setattr(
        studio,
        "claim_visual_scene_plan",
        Mock(return_value=(receipt, False)),
    )
    monkeypatch.setattr(studio, "build_visual_scene_bundle", build)

    restored_contract, source, restored_variants = asyncio.run(
        studio._ensure_scene_variants(state, data, actor=actor)
    )

    assert restored_contract == contract
    assert source == "ai"
    assert restored_variants == variants
    build.assert_not_called()


def test_durable_uncertain_scene_plan_fails_closed_to_deterministic_bundle(
    monkeypatch,
) -> None:
    monkeypatch.setattr(studio, "visual_scene_ai_planning_available", lambda: True)
    data = _data()
    for key in (
        "creative_scene_contract",
        "creative_scene_planner_source",
        "creative_scene_variants",
    ):
        data.pop(key, None)
    state = FakeState(data)
    receipt = SimpleNamespace(
        id="22222222-2222-4222-8222-222222222222",
        status=VisualScenePlanStatus.AMBIGUOUS,
        result_json="",
    )
    build = Mock(side_effect=AssertionError("ambiguous paid planner must not retry"))
    monkeypatch.setattr(
        studio,
        "claim_visual_scene_plan",
        Mock(return_value=(receipt, False)),
    )
    monkeypatch.setattr(studio, "build_visual_scene_bundle", build)

    contract, source, variants = asyncio.run(
        studio._ensure_scene_variants(
            state,
            data,
            actor=SimpleNamespace(),
        )
    )

    assert source == "deterministic"
    assert contract.primary_subject
    assert len(variants) == 5
    build.assert_not_called()




def test_claimed_scene_plan_completes_durable_bundle_before_cache(monkeypatch) -> None:
    monkeypatch.setattr(studio, "visual_scene_ai_planning_available", lambda: True)
    data = _data()
    contract = studio._scene_contract_from_state(data)
    variants = studio._scene_variants_from_state(data)
    for key in (
        "creative_scene_contract",
        "creative_scene_planner_source",
        "creative_scene_variants",
    ):
        data.pop(key, None)
    state = FakeState(data)
    actor = SimpleNamespace()
    receipt = SimpleNamespace(
        id="33333333-3333-4333-8333-333333333333",
        status=VisualScenePlanStatus.PLANNING,
        result_json="",
    )
    complete = Mock()
    monkeypatch.setattr(
        studio,
        "claim_visual_scene_plan",
        Mock(return_value=(receipt, True)),
    )
    monkeypatch.setattr(
        studio,
        "build_visual_scene_bundle",
        Mock(return_value=(contract, "ai", variants)),
    )
    monkeypatch.setattr(studio, "complete_visual_scene_plan", complete)

    restored_contract, source, restored_variants = asyncio.run(
        studio._ensure_scene_variants(state, data, actor=actor)
    )

    assert restored_contract == contract
    assert source == "ai"
    assert restored_variants == variants
    complete.assert_called_once()
    assert complete.call_args.kwargs["actor"] is actor
    assert complete.call_args.kwargs["receipt_id"] == receipt.id
    frozen = complete.call_args.kwargs["result_json"]
    assert '"planner_source":"ai"' in frozen
    assert state.data["creative_scene_planner_source"] == "ai"
    assert len(state.data["creative_scene_variants"]) == 5


def test_claimed_scene_plan_failures_never_retry_paid_planner(monkeypatch) -> None:
    monkeypatch.setattr(studio, "visual_scene_ai_planning_available", lambda: True)
    cases = (
        (OSError("transport"), None),
        (RuntimeError("runtime"), LookupError("already changed")),
        (ValueError("invalid"), ValueError("state changed")),
    )

    for index, (planning_error, mark_error) in enumerate(cases, start=1):
        data = _data()
        for key in (
            "creative_scene_contract",
            "creative_scene_planner_source",
            "creative_scene_variants",
        ):
            data.pop(key, None)
        state = FakeState(data)
        receipt = SimpleNamespace(
            id=f"44444444-4444-4444-8444-44444444444{index}",
            status=VisualScenePlanStatus.PLANNING,
            result_json="",
        )
        build = Mock(side_effect=planning_error)
        mark = (
            Mock()
            if mark_error is None
            else Mock(side_effect=mark_error)
        )
        monkeypatch.setattr(
            studio,
            "claim_visual_scene_plan",
            Mock(return_value=(receipt, True)),
        )
        monkeypatch.setattr(studio, "build_visual_scene_bundle", build)
        monkeypatch.setattr(studio, "mark_visual_scene_plan_ambiguous", mark)

        contract, source, variants = asyncio.run(
            studio._ensure_scene_variants(
                state,
                data,
                actor=SimpleNamespace(),
            )
        )

        assert source == "deterministic"
        assert contract.primary_subject
        assert len(variants) == 5
        build.assert_called_once()
        mark.assert_called_once()
        assert state.data["creative_scene_planner_source"] == "deterministic"


def test_ai_scene_bundle_preserves_grounded_meaning_and_real_art_direction() -> None:
    request = "ёж, который слушает ресурсное аудио и становится добрым и пушистым"
    flags = semantic_flags_for_request(request)
    compositions = (
        "clear_story",
        "cinematic",
        "editorial",
        "focused",
        "sequential",
    )
    variants = []
    for index, composition in enumerate(compositions, start=1):
        variants.append(
            {
                "title": f"Постановка {index}",
                "description": f"Осмысленный вариант {index} именно про ежа и его изменение.",
                "direction": (
                    f"Distinct art direction {index}: keep the hedgehog listening to the "
                    "resource audio while the visible emotional and fur transformation "
                    "develops coherently."
                ),
                "composition": composition,
                "provider_note": "harmless extra metadata",
            }
        )

    class FakeClient:
        def chat(self, messages, *, temperature, max_tokens):
            import json

            assert messages
            assert temperature == 0.55
            assert max_tokens == 1800
            return "```json\n" + json.dumps(
                {"variants": variants, "meta": {"provider": "yandex"}},
                ensure_ascii=False,
            ) + "\n```"

    contract, source, planned = build_visual_scene_bundle(
        request=request,
        semantic_flags=flags,
        style_intent=VisualStyleIntent(),
        client=FakeClient(),
    )

    assert source == "ai"
    assert len(planned) == 5
    assert all(item.source == "ai" for item in planned)
    assert all("Distinct art direction" in item.direction for item in planned)
    assert contract == fallback_scene_contract(request=request, semantic_flags=flags)
    assert "ёж" in request

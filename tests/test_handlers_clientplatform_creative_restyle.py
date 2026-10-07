from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from clientplatform.application import visual_creatives
from clientplatform.application.visual_scene_variants import VisualSceneVariant
from clientplatform.domain.visual_scene_contract import VisualSceneContract
from clientplatform.domain.visual_style_intent import VisualStyleIntent
from handlers import clientplatform_creative_studio as studio


def _scene() -> VisualSceneContract:
    return VisualSceneContract(
        version=1,
        topology="action",
        primary_subject="городская улица",
        initial_state=(),
        actions=("идут люди",),
        cause="",
        transition=(),
        final_state=(),
        explicit_text=(),
        required_evidence=("requested action visible",),
        forbidden=("unrelated subject", "unrequested readable text"),
    )


def _variant() -> VisualSceneVariant:
    return VisualSceneVariant(
        id="v2",
        title="Кинематографично",
        description="Та же улица с глубиной и естественным светом.",
        direction=(
            "Keep the same street, people and action. Use cinematic depth and "
            "natural motivated light."
        ),
        composition="cinematic",
        score=91,
        source="deterministic",
    )


class _State:
    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})
        self.set_state = AsyncMock()
        self.clear = AsyncMock()

    async def get_data(self) -> dict:
        return dict(self.data)

    async def set_data(self, value: dict) -> None:
        self.data = dict(value)

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)


def _locked_style_data() -> dict:
    return {
        "creative_business_id": "business-id",
        "creative_business_token": "business-token",
        "creative_kind": "image",
        "creative_pending_prompt": "люди идут по городской улице",
        "creative_brand_context": "",
        "creative_country_code": "RU",
        "creative_style_intent": VisualStyleIntent().to_mapping(),
        "creative_saved_style_applied": False,
        "creative_style_inferred_fields": [],
        "creative_style_only": True,
        "creative_locked_scene_contract": _scene().to_mapping(),
        "creative_locked_scene_planner_source": "deterministic",
        "creative_locked_scene_variant": _variant().to_mapping(),
    }


def test_frozen_visual_scene_round_trips_exact_contract_and_variant() -> None:
    scene = _scene()
    variant = _variant()
    frozen = visual_creatives.freeze_business_image_payload(
        request="люди идут по городской улице",
        country_code="RU",
        style_intent=VisualStyleIntent(quick_styles="cinematic"),
        scene_contract=scene,
        scene_planner_source="deterministic",
        scene_variant=variant,
    )

    restored = visual_creatives.frozen_business_visual_scene(frozen)

    assert restored is not None
    contract, source, selected = restored
    assert contract == scene
    assert source == "deterministic"
    assert selected == variant


def test_style_only_dashboard_removes_every_scene_replan_action() -> None:
    rows = studio._style_rows_for_session(
        studio.style_dashboard_rows("business-token", VisualStyleIntent()),
        _locked_style_data(),
    )
    callbacks = [callback for row in rows for _label, callback in row]

    assert any(value.startswith("cpc:st:go:") for value in callbacks)
    assert all(not value.startswith("cpc:sv:") for value in callbacks)


def test_style_only_prepare_reuses_frozen_scene_without_replanning(monkeypatch) -> None:
    data = _locked_style_data()
    state = _State(data)
    target = SimpleNamespace(answer=AsyncMock())
    actor = SimpleNamespace(
        business_id="business-id",
        assert_can_manage_promotions=lambda: None,
    )
    freeze = MagicMock(return_value="frozen-restyle")
    prepare = MagicMock(
        return_value=SimpleNamespace(
            id="receipt-id",
            request_text=data["creative_pending_prompt"],
            provider_payload_json="frozen-restyle",
        )
    )
    confirmation = AsyncMock()

    monkeypatch.setattr(studio.control, "_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(
        studio,
        "_ensure_scene_variants",
        AsyncMock(side_effect=AssertionError("style-only restyle must not replan")),
    )
    monkeypatch.setattr(studio, "freeze_business_image_payload", freeze)
    monkeypatch.setattr(studio, "prepare_creative_generation", prepare)
    monkeypatch.setattr(studio, "_show_paid_generation_confirmation", confirmation)

    asyncio.run(
        studio._prepare_styled_generation(
            target,
            state,
            user_id=101,
            token="business-token",
        )
    )

    assert freeze.call_count == 1
    kwargs = freeze.call_args.kwargs
    assert kwargs["scene_contract"] == _scene()
    assert kwargs["scene_planner_source"] == "deterministic"
    assert kwargs["scene_variant"] is not None
    assert kwargs["scene_variant"].composition == _variant().composition
    assert kwargs["scene_variant"].direction != _variant().direction
    assert "Do not prescribe lighting, palette, atmosphere or rendering style" in kwargs["scene_variant"].direction
    assert kwargs["override_owner_style_wording"] is True
    assert state.clear.await_count == 1
    assert confirmation.await_count == 1


def test_restyle_result_locks_original_scene_and_hides_scene_variants(monkeypatch) -> None:
    scene = _scene()
    variant = _variant()
    frozen = visual_creatives.freeze_business_image_payload(
        request="люди идут по городской улице",
        country_code="RU",
        style_intent=VisualStyleIntent(quick_styles="cinematic"),
        scene_contract=scene,
        scene_planner_source="deterministic",
        scene_variant=variant,
    )
    receipt = SimpleNamespace(
        request_text="люди идут по городской улице",
        brand_context="",
        country_code="RU",
        provider_payload_json=frozen,
    )
    actor = SimpleNamespace(business_id="business-id")
    target = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data="cpc:restyle:business-token:receipt-token",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=101),
        message=target,
    )
    state = _State()

    monkeypatch.setattr(studio, "_actor_for_callback", AsyncMock(return_value=actor))
    monkeypatch.setattr(studio, "_receipt_for_callback", AsyncMock(return_value=receipt))
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(studio.control, "_keyboard", lambda rows: rows)

    asyncio.run(studio.restyle_creative_result(callback, state))

    assert state.data["creative_style_only"] is True
    assert state.data["creative_locked_scene_contract"] == scene.to_mapping()
    assert state.data["creative_locked_scene_planner_source"] == "deterministic"
    assert state.data["creative_locked_scene_variant"] == variant.to_mapping()
    dashboard = target.answer.await_args.args[0]
    assert "постановка, объекты, действия и смысл сцены зафиксированы" in dashboard
    callbacks = [
        callback_data
        for row in target.answer.await_args.kwargs["reply_markup"]
        for _label, callback_data in row
    ]
    assert all(not value.startswith("cpc:sv:") for value in callbacks)
    assert any(value.startswith("cpc:st:go:") for value in callbacks)


def test_stale_scene_callback_is_blocked_during_style_only_restyle(monkeypatch) -> None:
    state = _State(_locked_style_data())
    callback = SimpleNamespace(
        data="cpc:sv:auto:business-token",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=101),
    )
    ensure = AsyncMock(side_effect=AssertionError("stale scene callback reached planner"))
    monkeypatch.setattr(studio, "_ensure_scene_variants", ensure)

    asyncio.run(studio.auto_scene_variant(callback, state))

    callback.answer.assert_awaited_once_with(
        "При смене стиля постановка зафиксирована и не меняется",
        show_alert=True,
    )
    assert ensure.await_count == 0

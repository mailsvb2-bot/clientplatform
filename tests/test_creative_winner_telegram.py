from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiogram.types import InlineKeyboardMarkup

from clientplatform.application.creative_growth_optimizer import (
    CreativeOptimizationArmEvidence,
    CreativeOptimizationMetric,
    CreativeOptimizationRecommendation,
    CreativeOptimizationStatus,
)
from clientplatform.application.creative_winner import (
    CreativeWinnerApplyError,
    CreativeWinnerApplyResult,
    CreativeWinnerPreview,
)
from clientplatform.domain.creative_growth import (
    CreativeAttributionScope,
    CreativeTrafficPlan,
    CreativeTrialArm,
    CreativeTrialStatus,
    CreativeVariantOutcome,
)
from clientplatform.presentation import creative_winner_telegram as ui


def _preview(*, status: CreativeOptimizationStatus = CreativeOptimizationStatus.READY) -> CreativeWinnerPreview:
    business_id = str(uuid4())
    arms = tuple(
        CreativeTrialArm(
            variant_id=f"variant-{index}",
            publication_job_id=str(uuid4()),
            allocation_bps=5000,
            promotion_campaign_id=str(uuid4()),
            promotion_source_token=f"variantSource{index}XYZ",
        )
        for index in range(2)
    )
    plan = CreativeTrafficPlan(
        trial_id=str(uuid4()),
        business_id=business_id,
        status=CreativeTrialStatus.RUNNING,
        revision=9,
        arms=arms,
    ).normalized()
    variants = (
        CreativeVariantOutcome(
            variant_id=arms[0].variant_id,
            publication_job_id=arms[0].publication_job_id,
            promotion_campaign_id=arms[0].promotion_campaign_id,
            attribution_scope=CreativeAttributionScope.VARIANT,
            leads=100,
            bookings=35,
            won=20,
        ),
        CreativeVariantOutcome(
            variant_id=arms[1].variant_id,
            publication_job_id=arms[1].publication_job_id,
            promotion_campaign_id=arms[1].promotion_campaign_id,
            attribution_scope=CreativeAttributionScope.VARIANT,
            leads=100,
            bookings=5,
            won=1,
        ),
    )
    evidence = (
        CreativeOptimizationArmEvidence(
            variant_id=arms[0].variant_id,
            publication_job_id=arms[0].publication_job_id,
            leads=100,
            successes=35,
            rate=0.35,
            confidence_low=0.26,
            confidence_high=0.45,
            current_allocation_bps=5000,
            proposed_allocation_bps=6000,
        ),
        CreativeOptimizationArmEvidence(
            variant_id=arms[1].variant_id,
            publication_job_id=arms[1].publication_job_id,
            leads=100,
            successes=5,
            rate=0.05,
            confidence_low=0.02,
            confidence_high=0.11,
            current_allocation_bps=5000,
            proposed_allocation_bps=4000,
        ),
    )
    recommendation = CreativeOptimizationRecommendation(
        trial_id=plan.trial_id,
        trial_revision=plan.revision,
        metric=CreativeOptimizationMetric.BOOKINGS,
        status=status,
        reason="test reason",
        winner_variant_id=arms[0].variant_id if status == CreativeOptimizationStatus.READY else "",
        evidence=evidence if status == CreativeOptimizationStatus.READY else (),
    )
    return CreativeWinnerPreview(
        plan=plan,
        variants=variants,
        recommendation=recommendation,
        date_from="2026-07-14",
        date_to="2026-08-12",
        fingerprint="0123456789abcdef",
    )


class _Message:
    def __init__(self) -> None:
        self.answers: list[tuple[str, object | None]] = []

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answers.append((text, reply_markup))


class _Callback:
    def __init__(self, data: str, message: _Message | None = None) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=701)
        self.message = message or _Message()
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class _State:
    def __init__(self) -> None:
        self.cleared = False

    async def clear(self) -> None:
        self.cleared = True


def _keyboard(rows):
    from aiogram.types import InlineKeyboardButton

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def _fake_control(preview: CreativeWinnerPreview):
    token_to_uuid = {
        "biz": preview.plan.business_id,
        "trial": preview.plan.trial_id,
    }
    uuid_to_token = {value: key for key, value in token_to_uuid.items()}

    async def actor(_user_id: int, business_id: str):
        return SimpleNamespace(business_id=business_id, user_id=701)

    return SimpleNamespace(
        _uuid_token=lambda value: uuid_to_token.get(value, "u" + value.replace("-", "")[:22]),
        _token_uuid=lambda value: token_to_uuid.get(value) or (_ for _ in ()).throw(ValueError()),
        _keyboard=_keyboard,
        _callback_message=lambda callback: callback.message,
        _actor=actor,
    )


def test_install_adds_review_only_to_advanced_keyboard(monkeypatch) -> None:
    preview = _preview()
    control = _fake_control(preview)
    included: list[object] = []

    def base_keyboard(_business_id: str, _capabilities: list[object]):
        return _keyboard([[('Базовая функция', 'base:1')]])

    simple = SimpleNamespace(
        _ADVANCED_KEYBOARD=base_keyboard,
        router=SimpleNamespace(include_router=lambda value: included.append(value)),
        _creative_winner_controls_installed=False,
    )
    monkeypatch.setattr(ui, "_CONTROL", None)

    ui.install_creative_winner_controls(control, simple)
    keyboard = simple._ADVANCED_KEYBOARD(preview.plan.business_id, [])
    texts = [button.text for row in keyboard.inline_keyboard for button in row]

    assert texts == ["Базовая функция", "🧪 A/B креативы"]
    assert included == [ui.router]
    ui.install_creative_winner_controls(control, simple)
    assert included == [ui.router]


def test_metric_tokens_are_strict() -> None:
    assert ui._metric_code(CreativeOptimizationMetric.BOOKINGS) == "b"
    assert ui._metric_code(CreativeOptimizationMetric.WON) == "w"
    assert ui._metric_from_code("b") == CreativeOptimizationMetric.BOOKINGS
    assert ui._metric_from_code("w") == CreativeOptimizationMetric.WON
    with pytest.raises(ValueError, match="unknown"):
        ui._metric_from_code("x")


def test_review_helpers_show_proposal_and_exact_evidence() -> None:
    preview = _preview()
    allocation = ui._allocation_lines(preview.plan, preview.recommendation)
    evidence = ui._evidence_lines(preview)

    assert "50% → 60%" in allocation
    assert "50% → 40%" in allocation
    assert "35/100" in evidence
    assert "5/100" in evidence
    assert "⭐" in evidence

    unavailable = replace(
        preview,
        variants=(
            replace(
                preview.variants[0],
                attribution_scope=CreativeAttributionScope.SHARED_CAMPAIGN,
            ),
            preview.variants[1],
        ),
    )
    assert "нет точной атрибуции" in ui._evidence_lines(unavailable)


def test_recommendation_text_covers_safe_hold_states() -> None:
    expected = {
        CreativeOptimizationStatus.READY: "статистически",
        CreativeOptimizationStatus.NOT_RUNNING: "не идёт",
        CreativeOptimizationStatus.ATTRIBUTION_NOT_READY: "нет точной атрибуции",
        CreativeOptimizationStatus.INSUFFICIENT_DATA: "недостаточно",
        CreativeOptimizationStatus.NO_CLEAR_WINNER: "не доказана",
        CreativeOptimizationStatus.EXPLORATION_FLOOR_REACHED: "минимальной",
    }
    for status, needle in expected.items():
        recommendation = replace(_preview(status=status).recommendation, status=status)
        assert needle in ui._recommendation_text(recommendation)


def test_send_review_embeds_evidence_fingerprint_within_callback_limit(monkeypatch) -> None:
    preview = _preview()
    control = _fake_control(preview)
    monkeypatch.setattr(ui, "_CONTROL", control)
    monkeypatch.setattr(ui, "preview_creative_winner", lambda **_kwargs: preview)
    message = _Message()

    asyncio.run(
        ui._send_trial_review(
            message,
            actor=SimpleNamespace(),
            trial_id=preview.plan.trial_id,
            metric=CreativeOptimizationMetric.BOOKINGS,
        )
    )

    text, markup = message.answers[-1]
    assert "ничего не" not in text.lower()
    assert "не меняет распределение сама" in text
    apply_button = markup.inline_keyboard[0][0]
    assert apply_button.text.startswith("✅ Применить")
    assert preview.fingerprint in str(apply_button.callback_data)
    assert len(str(apply_button.callback_data).encode("utf-8")) <= 64


def test_home_lists_trials_without_mutation(monkeypatch) -> None:
    preview = _preview()
    control = _fake_control(preview)
    monkeypatch.setattr(ui, "_CONTROL", control)
    monkeypatch.setattr(ui, "list_creative_trials", lambda **_kwargs: (preview.plan,))
    callback = _Callback("cpw:home:biz")
    state = _State()

    asyncio.run(ui.open_creative_trials(callback, state))

    assert state.cleared is True
    assert callback.answers[-1] == (None, False)
    text, markup = callback.message.answers[-1]
    assert "точной variant-attribution" in text
    assert markup.inline_keyboard[0][0].callback_data == "cpw:trial:trial:b"


def test_invalid_trial_and_apply_callbacks_are_rejected() -> None:
    preview = _preview()
    ui._CONTROL = _fake_control(preview)

    invalid_trial = _Callback("cpw:trial:trial:x")
    asyncio.run(ui.open_creative_trial(invalid_trial))
    assert invalid_trial.answers[-1][1] is True

    invalid_apply = _Callback("cpw:apply:too:short")
    asyncio.run(ui.apply_creative_trial_winner(invalid_apply))
    assert invalid_apply.answers[-1] == ("Подтверждение устарело", True)


def test_apply_reports_stale_evidence_without_success_message(monkeypatch) -> None:
    preview = _preview()
    control = _fake_control(preview)
    monkeypatch.setattr(ui, "_CONTROL", control)
    monkeypatch.setattr(
        ui,
        "resolve_creative_trial_actor",
        lambda **_kwargs: SimpleNamespace(),
    )

    def stale(**_kwargs):
        raise CreativeWinnerApplyError("creative winner evidence changed")

    monkeypatch.setattr(ui, "apply_creative_winner", stale)
    callback = _Callback("cpw:apply:trial:9:b:0123456789abcdef")

    asyncio.run(ui.apply_creative_trial_winner(callback))

    assert callback.answers[-1][1] is True
    assert "не актуальна" in str(callback.answers[-1][0])
    assert callback.message.answers == []


def test_apply_success_shows_new_revision_and_allocation(monkeypatch) -> None:
    preview = _preview()
    control = _fake_control(preview)
    monkeypatch.setattr(ui, "_CONTROL", control)
    monkeypatch.setattr(
        ui,
        "resolve_creative_trial_actor",
        lambda **_kwargs: SimpleNamespace(),
    )
    proposed = {
        item.publication_job_id: item.proposed_allocation_bps
        for item in preview.recommendation.evidence
    }
    updated = replace(
        preview.plan,
        revision=preview.plan.revision + 1,
        arms=tuple(
            replace(arm, allocation_bps=proposed[arm.publication_job_id])
            for arm in preview.plan.arms
        ),
    ).normalized()
    monkeypatch.setattr(
        ui,
        "apply_creative_winner",
        lambda **_kwargs: CreativeWinnerApplyResult(preview=preview, updated_plan=updated),
    )
    callback = _Callback("cpw:apply:trial:9:b:0123456789abcdef")

    asyncio.run(ui.apply_creative_trial_winner(callback))

    assert callback.answers[-1] == ("Распределение обновлено", False)
    text, _markup = callback.message.answers[-1]
    assert "Новая версия теста: 10" in text
    assert "60%" in text
    assert "40%" in text

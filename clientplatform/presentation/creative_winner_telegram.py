from __future__ import annotations

import asyncio
from types import ModuleType

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.creative_winner import (
    CreativeWinnerApplyError,
    apply_creative_winner,
    list_creative_trials,
    preview_creative_winner,
    resolve_creative_trial_actor,
)
from clientplatform.domain.creative_growth import CreativeTrafficPlan, CreativeTrialStatus
from clientplatform.domain.creative_winner_policy import (
    CreativeWinnerDecision,
    CreativeWinnerMetric,
    CreativeWinnerRecommendation,
)
from clientplatform.domain.tenancy import TenantPermissionDenied, TenancyError


router = Router(name="clientplatform_creative_winner")
_CONTROL: ModuleType | None = None

_STATUS_LABELS = {
    CreativeTrialStatus.DRAFT: "черновик",
    CreativeTrialStatus.RUNNING: "идёт",
    CreativeTrialStatus.PAUSED: "на паузе",
    CreativeTrialStatus.COMPLETED: "завершён",
}
_REASON_TEXT = {
    "trial_not_running": "Тест сейчас не идёт — распределение не меняю.",
    "exact_variant_attribution_required": (
        "Для части вариантов нет точной атрибуции. Выбирать победителя по общим "
        "данным кампании было бы нечестно — распределение не меняю."
    ),
    "minimum_attributed_opens_not_reached": (
        "Пока мало точных переходов: нужно минимум 30 на каждый вариант."
    ),
    "minimum_conversion_events_not_reached": (
        "Пока мало записей или выигранных сделок для надёжного решения."
    ),
    "top_conversion_rates_are_tied": "У лидирующих вариантов одинаковая конверсия.",
    "conversion_delta_below_policy_threshold": (
        "Разница есть, но она меньше безопасного порога в 5 процентных пунктов."
    ),
    "confidence_intervals_overlap": (
        "Разница пока статистически не отделилась: доверительные интервалы пересекаются."
    ),
    "allocation_policy_bound_reached": (
        "Лидер уже достиг безопасного потолка распределения; дополнительный сдвиг не предлагаю."
    ),
    "statistically_separated_conversion_rate": (
        "Есть устойчиво отделившийся лидер. Можно осторожно сдвинуть не более 10 п.п. "
        "трафика, сохранив контрольную долю у остальных вариантов."
    ),
}


def _control() -> ModuleType:
    if _CONTROL is None:
        raise RuntimeError("creative winner Telegram controls are not installed")
    return _CONTROL


def _message(callback: CallbackQuery) -> Message:
    return _control()._callback_message(callback)


def install_creative_winner_controls(
    control_module: ModuleType,
    simple_module: ModuleType,
) -> None:
    """Add evidence-aware A/B review to the advanced owner dashboard."""

    global _CONTROL
    if bool(getattr(control_module, "_creative_winner_controls_installed", False)):
        return
    _CONTROL = control_module
    original = control_module._dashboard_keyboard

    def _dashboard_with_creative_winner(business_id: str, capabilities: list[object]):
        base = original(business_id, capabilities)
        rows = [list(row) for row in base.inline_keyboard]
        button = [
            InlineKeyboardButton(
                text="🧪 A/B креативы",
                callback_data=f"cpw:home:{control_module._uuid_token(business_id)}",
            )
        ]
        insert_at = max(0, len(rows) - 1)
        rows.insert(insert_at, button)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    control_module._dashboard_keyboard = _dashboard_with_creative_winner
    simple_module._ADVANCED_KEYBOARD = _dashboard_with_creative_winner
    simple_module.router.include_router(router)
    control_module._creative_winner_controls_installed = True


def _allocation_lines(
    plan: CreativeTrafficPlan,
    recommendation: CreativeWinnerRecommendation | None = None,
) -> str:
    proposed = (
        dict(recommendation.recommended_allocations)
        if recommendation is not None
        else {}
    )
    lines: list[str] = []
    for index, arm in enumerate(plan.arms, start=1):
        current = arm.allocation_bps / 100.0
        target = proposed.get(arm.publication_job_id, arm.allocation_bps) / 100.0
        if target == current:
            lines.append(f"• Вариант {index}: {current:.0f}%")
        else:
            lines.append(f"• Вариант {index}: {current:.0f}% → {target:.0f}%")
    return "\n".join(lines)


def _evidence_lines(
    plan: CreativeTrafficPlan,
    recommendation: CreativeWinnerRecommendation,
) -> str:
    if not recommendation.evidence:
        return ""
    by_variant = {item.variant_id: item for item in recommendation.evidence}
    metric_label = (
        "выигранных сделок"
        if recommendation.metric == CreativeWinnerMetric.WON
        else "записей"
    )
    lines = [f"\nДоказательства · {metric_label} / точные переходы:"]
    for index, arm in enumerate(plan.arms, start=1):
        item = by_variant[arm.variant_id]
        lines.append(
            f"• Вариант {index}: {item.successes}/{item.leads} · {item.rate * 100:.1f}%"
        )
    return "\n".join(lines)


async def _send_trial_review(
    message: Message,
    *,
    actor,
    trial_id: str,
) -> None:
    preview = await asyncio.to_thread(
        preview_creative_winner,
        actor=actor,
        trial_id=trial_id,
    )
    recommendation = preview.recommendation
    plan = await asyncio.to_thread(
        _load_plan,
        actor=actor,
        trial_id=trial_id,
    )
    trial_token = _control()._uuid_token(plan.trial_id)
    business_token = _control()._uuid_token(plan.business_id)
    reason = _REASON_TEXT.get(recommendation.reason, recommendation.reason)
    rows: list[list[tuple[str, str]]] = []
    if recommendation.can_apply:
        rows.append(
            [
                (
                    "✅ Применить осторожный сдвиг",
                    "cpw:apply:"
                    f"{trial_token}:{recommendation.expected_revision}:{preview.fingerprint}",
                )
            ]
        )
    rows.extend(
        [
            [("🔄 Пересчитать", f"cpw:trial:{trial_token}")],
            [("⬅️ Все A/B тесты", f"cpw:home:{business_token}")],
            [("⚙️ Все возможности", f"cps:advanced:{business_token}")],
        ]
    )
    await message.answer(
        "🧪 A/B креативы · рекомендация\n\n"
        f"Статус теста: {_STATUS_LABELS[plan.status]} · версия {plan.revision}\n"
        f"Период данных: {preview.date_from} — {preview.date_to}\n\n"
        f"{reason}\n\n"
        f"Распределение:\n{_allocation_lines(plan, recommendation)}"
        f"{_evidence_lines(plan, recommendation)}\n\n"
        "ClientPlatform ничего не перераспределяет сама: изменение применяется "
        "только после отдельного подтверждения и только если данные не изменились.",
        reply_markup=_control()._keyboard(rows),
    )


def _load_plan(*, actor, trial_id: str) -> CreativeTrafficPlan:
    # Keep presentation reads behind the same application entry point used by
    # the list view, avoiding direct database access in Telegram handlers.
    plans = list_creative_trials(actor=actor)
    for plan in plans:
        if plan.trial_id == trial_id:
            return plan
    raise LookupError("creative growth trial was not found")


@router.callback_query(F.data.startswith("cpw:home:"))
async def open_creative_trials(callback: CallbackQuery, state: FSMContext) -> None:
    c = _control()
    await state.clear()
    business_token = str(callback.data).split(":", 2)[2]
    business_id = c._token_uuid(business_token)
    try:
        actor = await c._actor(int(callback.from_user.id), business_id)
        plans = await asyncio.to_thread(list_creative_trials, actor=actor)
    except (TenantPermissionDenied, TenancyError, LookupError, RuntimeError, ValueError):
        await callback.answer("Не удалось открыть A/B тесты", show_alert=True)
        return
    await callback.answer()
    if not plans:
        await _message(callback).answer(
            "🧪 A/B креативы\n\n"
            "Пока нет созданных тестов креативов. Здесь появятся результаты тестов "
            "с несколькими вариантами рекламы; общие данные одной кампании не будут "
            "выдаваться за победу отдельного варианта.",
            reply_markup=c._keyboard(
                [[("⚙️ Все возможности", f"cps:advanced:{business_token}")]]
            ),
        )
        return
    rows = [
        [
            (
                f"🧪 Тест {index} · {_STATUS_LABELS[plan.status]} · {len(plan.arms)} варианта",
                f"cpw:trial:{c._uuid_token(plan.trial_id)}",
            )
        ]
        for index, plan in enumerate(plans[:10], start=1)
    ]
    rows.append([("⚙️ Все возможности", f"cps:advanced:{business_token}")])
    await _message(callback).answer(
        "🧪 A/B креативы\n\n"
        "Выберите тест. ClientPlatform сравнит только точно атрибутированные "
        "переходы, записи и выигранные сделки и не станет менять распределение "
        "при слабых данных.",
        reply_markup=c._keyboard(rows),
    )


@router.callback_query(F.data.startswith("cpw:trial:"))
async def open_creative_trial(callback: CallbackQuery) -> None:
    c = _control()
    raw = str(callback.data).split(":", 2)[2]
    try:
        trial_id = c._token_uuid(raw)
        actor = await asyncio.to_thread(
            resolve_creative_trial_actor,
            user_id=int(callback.from_user.id),
            trial_id=trial_id,
        )
        await callback.answer()
        await _send_trial_review(_message(callback), actor=actor, trial_id=trial_id)
    except (TenantPermissionDenied, TenancyError, LookupError, RuntimeError, ValueError):
        await callback.answer("Не удалось открыть рекомендацию", show_alert=True)


@router.callback_query(F.data.startswith("cpw:apply:"))
async def apply_creative_trial_winner(callback: CallbackQuery) -> None:
    c = _control()
    parts = str(callback.data).split(":")
    if len(parts) != 5:
        await callback.answer("Подтверждение устарело", show_alert=True)
        return
    _, _, trial_token, raw_revision, fingerprint = parts
    try:
        trial_id = c._token_uuid(trial_token)
        revision = int(raw_revision)
        actor = await asyncio.to_thread(
            resolve_creative_trial_actor,
            user_id=int(callback.from_user.id),
            trial_id=trial_id,
        )
        result = await asyncio.to_thread(
            apply_creative_winner,
            actor=actor,
            trial_id=trial_id,
            expected_revision=revision,
            expected_fingerprint=fingerprint,
        )
    except CreativeWinnerApplyError:
        await callback.answer(
            "Данные изменились или рекомендация уже не актуальна. Пересчитайте её.",
            show_alert=True,
        )
        return
    except TenantPermissionDenied:
        await callback.answer("Недостаточно прав для изменения A/B теста", show_alert=True)
        return
    except (TenancyError, LookupError, RuntimeError, ValueError):
        await callback.answer("Не удалось применить рекомендацию", show_alert=True)
        return

    await callback.answer("Распределение обновлено")
    plan = result.updated_plan
    business_token = c._uuid_token(plan.business_id)
    await _message(callback).answer(
        "✅ Осторожный сдвиг применён\n\n"
        f"Новая версия теста: {plan.revision}\n"
        f"Распределение:\n{_allocation_lines(plan)}\n\n"
        "Контрольная доля у остальных вариантов сохранена. Следующее изменение "
        "потребует новых данных и отдельного подтверждения.",
        reply_markup=c._keyboard(
            [
                [("🔄 Новая рекомендация", f"cpw:trial:{c._uuid_token(plan.trial_id)}")],
                [("🧪 Все A/B тесты", f"cpw:home:{business_token}")],
            ]
        ),
    )


__all__ = [
    "apply_creative_trial_winner",
    "install_creative_winner_controls",
    "open_creative_trial",
    "open_creative_trials",
    "router",
]

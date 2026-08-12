from __future__ import annotations

import asyncio
from types import ModuleType

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.creative_growth_optimizer import (
    CreativeOptimizationMetric,
    CreativeOptimizationRecommendation,
    CreativeOptimizationStatus,
)
from clientplatform.application.creative_winner import (
    CreativeWinnerApplyError,
    CreativeWinnerPreview,
    apply_creative_winner,
    list_creative_trials,
    preview_creative_winner,
    resolve_creative_trial_actor,
)
from clientplatform.domain.creative_growth import CreativeAttributionScope, CreativeTrafficPlan, CreativeTrialStatus
from clientplatform.domain.tenancy import TenantPermissionDenied, TenancyError


router = Router(name="clientplatform_creative_winner")
_CONTROL: ModuleType | None = None

_STATUS_LABELS = {
    CreativeTrialStatus.DRAFT: "черновик",
    CreativeTrialStatus.RUNNING: "идёт",
    CreativeTrialStatus.PAUSED: "на паузе",
    CreativeTrialStatus.COMPLETED: "завершён",
}


def _control() -> ModuleType:
    if _CONTROL is None:
        raise RuntimeError("creative winner Telegram controls are not installed")
    return _CONTROL


def _message(callback: CallbackQuery) -> Message:
    return _control()._callback_message(callback)


def _metric_code(metric: CreativeOptimizationMetric) -> str:
    return "w" if metric == CreativeOptimizationMetric.WON else "b"


def _metric_from_code(value: str) -> CreativeOptimizationMetric:
    if value == "b":
        return CreativeOptimizationMetric.BOOKINGS
    if value == "w":
        return CreativeOptimizationMetric.WON
    raise ValueError("unknown creative optimization metric")


def install_creative_winner_controls(
    control_module: ModuleType,
    simple_module: ModuleType,
) -> None:
    """Expose A/B review only inside the existing advanced owner dashboard."""

    global _CONTROL
    if bool(getattr(simple_module, "_creative_winner_controls_installed", False)):
        return
    _CONTROL = control_module
    original = simple_module._ADVANCED_KEYBOARD
    if original is None:
        raise RuntimeError("creative winner controls require the advanced dashboard")

    def _advanced_with_creative_winner(business_id: str, capabilities: list[object]):
        base = original(business_id, capabilities)
        rows = [list(row) for row in base.inline_keyboard]
        rows.append(
            [
                InlineKeyboardButton(
                    text="🧪 A/B креативы",
                    callback_data=f"cpw:home:{control_module._uuid_token(business_id)}",
                )
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    simple_module._ADVANCED_KEYBOARD = _advanced_with_creative_winner
    simple_module.router.include_router(router)
    simple_module._creative_winner_controls_installed = True


def _allocation_lines(
    plan: CreativeTrafficPlan,
    recommendation: CreativeOptimizationRecommendation | None = None,
) -> str:
    proposed = {
        item.publication_job_id: item.proposed_allocation_bps
        for item in (() if recommendation is None else recommendation.evidence)
    }
    lines: list[str] = []
    for index, arm in enumerate(plan.arms, start=1):
        current = arm.allocation_bps / 100.0
        target = proposed.get(arm.publication_job_id, arm.allocation_bps) / 100.0
        if target == current:
            lines.append(f"• Вариант {index}: {current:.0f}%")
        else:
            lines.append(f"• Вариант {index}: {current:.0f}% → {target:.0f}%")
    return "\n".join(lines)


def _evidence_lines(preview: CreativeWinnerPreview) -> str:
    by_variant = {item.variant_id: item for item in preview.variants}
    recommendation = preview.recommendation
    metric = recommendation.metric
    metric_label = "продаж" if metric == CreativeOptimizationMetric.WON else "записей"
    lines = [f"\nДанные · {metric_label} / точные переходы:"]
    for index, arm in enumerate(preview.plan.arms, start=1):
        item = by_variant.get(arm.variant_id)
        if item is None or item.attribution_scope != CreativeAttributionScope.VARIANT:
            lines.append(f"• Вариант {index}: нет точной атрибуции")
            continue
        successes = item.won if metric == CreativeOptimizationMetric.WON else item.bookings
        rate = (successes / item.leads * 100.0) if item.leads else 0.0
        marker = " ⭐" if arm.variant_id == recommendation.winner_variant_id else ""
        lines.append(
            f"• Вариант {index}: {successes}/{item.leads} · {rate:.1f}%{marker}"
        )
    return "\n".join(lines)


def _recommendation_text(recommendation: CreativeOptimizationRecommendation) -> str:
    if recommendation.status == CreativeOptimizationStatus.READY:
        return (
            "Есть статистически отделившийся лидер. Можно применить один осторожный "
            "сдвиг распределения, не меняя общий рекламный бюджет."
        )
    if recommendation.status == CreativeOptimizationStatus.NOT_RUNNING:
        return "Тест сейчас не идёт — распределение не меняю."
    if recommendation.status == CreativeOptimizationStatus.ATTRIBUTION_NOT_READY:
        return (
            "Для части вариантов нет точной атрибуции. Общие данные кампании не будут "
            "выдаваться за победу отдельного креатива."
        )
    if recommendation.status == CreativeOptimizationStatus.INSUFFICIENT_DATA:
        return "Пока недостаточно точных переходов для безопасного решения."
    if recommendation.status == CreativeOptimizationStatus.NO_CLEAR_WINNER:
        return "Разница пока не доказана: 95% доверительные интервалы ещё пересекаются."
    if recommendation.status == CreativeOptimizationStatus.EXPLORATION_FLOOR_REACHED:
        return "Проигрывающие варианты уже достигли минимальной контрольной доли."
    return recommendation.reason


async def _send_trial_review(
    message: Message,
    *,
    actor,
    trial_id: str,
    metric: CreativeOptimizationMetric,
) -> None:
    preview = await asyncio.to_thread(
        preview_creative_winner,
        actor=actor,
        trial_id=trial_id,
        metric=metric,
    )
    recommendation = preview.recommendation
    plan = preview.plan
    trial_token = _control()._uuid_token(plan.trial_id)
    business_token = _control()._uuid_token(plan.business_id)
    metric_code = _metric_code(metric)
    rows: list[list[tuple[str, str]]] = []
    if recommendation.can_apply:
        rows.append(
            [
                (
                    "✅ Применить осторожный сдвиг",
                    "cpw:apply:"
                    f"{trial_token}:{recommendation.trial_revision}:{metric_code}:{preview.fingerprint}",
                )
            ]
        )
    rows.extend(
        [
            [
                ("📅 По записям", f"cpw:trial:{trial_token}:b"),
                ("💰 По продажам", f"cpw:trial:{trial_token}:w"),
            ],
            [("🔄 Пересчитать", f"cpw:trial:{trial_token}:{metric_code}")],
            [("⬅️ Все A/B тесты", f"cpw:home:{business_token}")],
            [("⚙️ Все возможности", f"cps:advanced:{business_token}")],
        ]
    )
    await message.answer(
        "🧪 A/B креативы · рекомендация\n\n"
        f"Статус теста: {_STATUS_LABELS[plan.status]} · версия {plan.revision}\n"
        f"Период данных: {preview.date_from} — {preview.date_to}\n\n"
        f"{_recommendation_text(recommendation)}\n\n"
        f"Распределение:\n{_allocation_lines(plan, recommendation)}"
        f"{_evidence_lines(preview)}\n\n"
        "ClientPlatform не меняет распределение сама. Кнопка применения каждый раз "
        "пересчитывает данные; если факты или версия теста изменились, старое "
        "подтверждение будет отклонено.",
        reply_markup=_control()._keyboard(rows),
    )


@router.callback_query(F.data.startswith("cpw:home:"))
async def open_creative_trials(callback: CallbackQuery, state: FSMContext) -> None:
    c = _control()
    await state.clear()
    business_token = str(callback.data).split(":", 2)[2]
    try:
        business_id = c._token_uuid(business_token)
    except ValueError:
        await callback.answer("Кнопка устарела. Откройте A/B тесты заново.", show_alert=True)
        return
    try:
        actor = await c._actor(int(callback.from_user.id), business_id)
        plans = await asyncio.to_thread(list_creative_trials, actor=actor)
    except TenancyError:
        await callback.answer("Недостаточно прав для просмотра A/B тестов", show_alert=True)
        return
    await callback.answer()
    if not plans:
        await _message(callback).answer(
            "🧪 A/B креативы\n\n"
            "Пока нет созданных тестов. Когда у рекламы появятся несколько креативов, "
            "здесь можно будет сравнивать их по точным переходам, записям и продажам.",
            reply_markup=c._keyboard(
                [[("⚙️ Все возможности", f"cps:advanced:{business_token}")]]
            ),
        )
        return
    rows = [
        [
            (
                f"🧪 Тест {index} · {_STATUS_LABELS[plan.status]} · {len(plan.arms)} варианта",
                f"cpw:trial:{c._uuid_token(plan.trial_id)}:b",
            )
        ]
        for index, plan in enumerate(plans[:10], start=1)
    ]
    rows.append([("⚙️ Все возможности", f"cps:advanced:{business_token}")])
    await _message(callback).answer(
        "🧪 A/B креативы\n\n"
        "Выберите тест. Решение строится только на точной variant-attribution; "
        "слабые или общие данные кампании не превращаются в ложного победителя.",
        reply_markup=c._keyboard(rows),
    )


@router.callback_query(F.data.startswith("cpw:trial:"))
async def open_creative_trial(callback: CallbackQuery) -> None:
    c = _control()
    parts = str(callback.data).split(":")
    if len(parts) != 4:
        await callback.answer("Кнопка устарела. Откройте A/B тест заново.", show_alert=True)
        return
    _, _, trial_token, metric_token = parts
    try:
        trial_id = c._token_uuid(trial_token)
        metric = _metric_from_code(metric_token)
    except ValueError:
        await callback.answer("Кнопка устарела. Откройте A/B тест заново.", show_alert=True)
        return
    try:
        actor = await asyncio.to_thread(
            resolve_creative_trial_actor,
            user_id=int(callback.from_user.id),
            trial_id=trial_id,
        )
        await callback.answer()
        await _send_trial_review(
            _message(callback),
            actor=actor,
            trial_id=trial_id,
            metric=metric,
        )
    except TenantPermissionDenied:
        await callback.answer("Недостаточно прав для просмотра A/B теста", show_alert=True)
    except TenancyError:
        await callback.answer("Не удалось подтвердить доступ к A/B тесту", show_alert=True)
    except LookupError:
        await callback.answer("A/B тест больше не найден", show_alert=True)


@router.callback_query(F.data.startswith("cpw:apply:"))
async def apply_creative_trial_winner(callback: CallbackQuery) -> None:
    c = _control()
    parts = str(callback.data).split(":")
    if len(parts) != 6:
        await callback.answer("Подтверждение устарело", show_alert=True)
        return
    _, _, trial_token, raw_revision, metric_token, fingerprint = parts
    try:
        trial_id = c._token_uuid(trial_token)
        revision = int(raw_revision)
        metric = _metric_from_code(metric_token)
    except ValueError:
        await callback.answer("Подтверждение устарело", show_alert=True)
        return
    try:
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
            confirmed=True,
            metric=metric,
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
    except TenancyError:
        await callback.answer("Не удалось подтвердить права на A/B тест", show_alert=True)
        return
    except LookupError:
        await callback.answer("A/B тест больше не найден", show_alert=True)
        return

    await callback.answer("Распределение обновлено")
    plan = result.updated_plan
    business_token = c._uuid_token(plan.business_id)
    await _message(callback).answer(
        "✅ Осторожный сдвиг применён\n\n"
        f"Новая версия теста: {plan.revision}\n"
        f"Распределение:\n{_allocation_lines(plan)}\n\n"
        "Контрольная доля остальных вариантов сохранена. Следующее изменение "
        "потребует новых данных и отдельного подтверждения.",
        reply_markup=c._keyboard(
            [
                [
                    (
                        "🔄 Новая рекомендация",
                        f"cpw:trial:{c._uuid_token(plan.trial_id)}:{_metric_code(metric)}",
                    )
                ],
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

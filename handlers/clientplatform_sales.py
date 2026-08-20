from __future__ import annotations

"""Plain-language owner UI for the tenant-scoped ClientPlatform sales core."""

import asyncio
import importlib

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from clientplatform.application.commercial_ladder import (
    add_commercial_ladder_step,
    create_commercial_ladder,
)
from clientplatform.application.sales_handoff import (
    claim_sales_handoff,
    resolve_sales_handoff,
)
from clientplatform.application.retention import (
    RetentionCandidateUnavailable,
    list_retention_candidates,
    prepare_reactivation_sales_lead,
)
from clientplatform.application.sales_metrics import get_sales_funnel_snapshot
from clientplatform.domain.retention import RetentionCohort
from clientplatform.domain.sales import SalesInvariantViolation
from clientplatform.domain.tenancy import TenantAccessDenied, TenantPermissionDenied
from clientplatform.application.sales_orchestration import (
    approve_and_authorize_sales_outbound,
)
from clientplatform.application.sales_ui import (
    list_commercial_ladder_steps,
    list_commercial_ladders,
    list_sales_handoff_work,
    list_sales_work,
)

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_sales")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())


class ClientPlatformSalesUiState(StatesGroup):
    ladder_name = State()
    ladder_step_title = State()


_STAGE_LABELS = {
    "new": "Новый интерес",
    "contacted": "На связи",
    "qualified": "Готов к предложению",
    "checkout": "Оформление",
}
_ACTION_LABELS = {
    "respond": "Ответить на обращение",
    "ask_qualification": "Уточнить задачу",
    "present_offer": "Предложить подходящую услугу",
    "checkout_followup": "Помочь завершить оформление",
    "human_handoff": "Подключиться лично",
    "noop": "Действий не требуется",
}
_HANDOFF_REASON_LABELS = {
    "explicit_request": "Клиент попросил человека",
    "low_confidence": "Нужна ручная проверка",
    "sensitive_context": "Требуется личное внимание",
    "pricing_exception": "Нестандартные условия",
    "negative_sentiment": "Клиент недоволен",
    "repeated_failure": "Автоматический сценарий не справился",
}
_SEVERITY_LABELS = {
    "urgent": "🔴 Срочно",
    "high": "🟠 Важно",
    "normal": "🟢 Обычно",
}
_LADDER_KIND_LABELS = {
    "diagnostic": "Диагностика",
    "audit": "Разбор / аудит",
    "implementation": "Основная услуга",
    "recurring": "Сопровождение",
}
_KIND_CODES = {
    "d": "diagnostic",
    "a": "audit",
    "i": "implementation",
    "r": "recurring",
}
_SOURCE_LABELS = {
    "telegram": "Telegram",
    "vk": "VK",
    "max": "MAX",
    "website": "Сайт",
    "referral": "Рекомендации",
    "manual": "Вручную",
}
_RETENTION_COHORT_LABELS = {
    RetentionCohort.ONE_TIME_CUSTOMER: "Покупал один раз",
    RetentionCohort.INACTIVE_CUSTOMER: "Давно не возвращался",
}
_RETENTION_ACTION_LABELS = {
    "review_repeat_offer": "Подготовить повторное предложение",
    "review_reactivation_offer": "Подготовить предложение для возврата",
}
_RETENTION_COHORT_CODES = {
    RetentionCohort.ONE_TIME_CUSTOMER: "o",
    RetentionCohort.INACTIVE_CUSTOMER: "i",
}
_RETENTION_CODE_COHORTS = {value: key for key, value in _RETENTION_COHORT_CODES.items()}


def _token(value: str) -> str:
    return control._uuid_token(value)


def _uuid(value: str) -> str:
    return control._token_uuid(value)


def _source_label(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return _SOURCE_LABELS.get(normalized, normalized or "Другой источник")


def _home_keyboard(business_id: str):
    token = _token(business_id)
    return control._keyboard(
        [
            [("💬 Обращения", f"cps:sw:{token}"), ("🙋 Нужно подключиться", f"cps:sh:{token}")],
            [("📊 Как идут продажи", f"cps:sf:{token}"), ("🧩 Что предлагать", f"cps:sl:{token}")],
            [("♻️ Вернуть клиентов", f"cps:sr:{token}")],
            [("🏠 В кабинет", f"cp:business:{token}")],
        ]
    )


def _back_keyboard(business_id: str):
    return control._keyboard(
        [[("← Обращения и продажи", f"cps:s:{_token(business_id)}")]]
    )


async def send_sales_home(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    actor = await control._actor(user_id, business_id)
    handoffs, snapshot = await asyncio.gather(
        asyncio.to_thread(list_sales_handoff_work, actor=actor, limit=50),
        asyncio.to_thread(get_sales_funnel_snapshot, actor=actor),
    )
    active_count = max(
        0,
        snapshot.total.discovered - snapshot.total.won - snapshot.total.lost,
    )
    await message.answer(
        "💬 Обращения и продажи\n\n"
        "Здесь собраны люди, которые уже проявили интерес или написали Вам. "
        "ClientPlatform показывает, что лучше сделать дальше, и может подготовить "
        "черновик ответа с помощью ИИ. Ничего не отправляется клиенту без Вашего "
        "подтверждения.\n\n"
        f"Активных обращений: {active_count}\n"
        f"Нужно подключиться лично: {len(handoffs)}\n"
        f"Оплатили: {snapshot.total.won}",
        reply_markup=_home_keyboard(business_id),
    )


@router.callback_query(F.data.startswith("cps:s:"))
async def open_sales_home(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = _uuid(str(callback.data).split(":", 2)[2])
    await state.clear()
    await callback.answer()
    await send_sales_home(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


async def _send_retention_candidates(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    actor = await control._actor(user_id, business_id)
    candidates = await asyncio.to_thread(
        list_retention_candidates,
        actor=actor,
        limit=8,
    )
    business_token = _token(business_id)
    rows: list[list[tuple[str, str]]] = []
    if not candidates:
        text = (
            "♻️ Вернуть клиентов\n\n"
            "Сейчас нет клиентов, которых ClientPlatform может обоснованно предложить "
            "для возврата по подтверждённой истории покупок и активности."
        )
    else:
        lines = [
            "♻️ Вернуть клиентов",
            "",
            "Здесь только клиенты с подтверждённой историей. Система ничего не отправляет "
            "сама: сначала Вы выбираете, кого взять в работу.",
            "",
        ]
        for index, candidate in enumerate(candidates, start=1):
            cohort_label = _RETENTION_COHORT_LABELS[candidate.cohort]
            action_label = _RETENTION_ACTION_LABELS[candidate.suggested_action.value]
            lines.extend(
                [
                    f"{index}. {candidate.display_name or 'Клиент'}",
                    f"   Почему здесь: {cohort_label}",
                    f"   Без активности: {candidate.inactive_days} дн.",
                    f"   Предлагаемый шаг: {action_label}",
                    "",
                ]
            )
            code = _RETENTION_COHORT_CODES[candidate.cohort]
            rows.append(
                [
                    (
                        f"✅ Взять в работу {index}",
                        f"cps:srr:{business_token}:{_token(candidate.customer_id)}:{code}",
                    )
                ]
            )
        text = "\n".join(lines).rstrip()
    rows.append([("← Обращения и продажи", f"cps:s:{business_token}")])
    await message.answer(text, reply_markup=control._keyboard(rows))


@router.callback_query(F.data.startswith("cps:sr:"))
async def open_retention_candidates(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = _uuid(str(callback.data).split(":", 2)[2])
    await state.clear()
    await callback.answer()
    await _send_retention_candidates(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cps:srr:"))
async def approve_retention_candidate(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    if len(parts) != 5 or parts[4] not in _RETENTION_CODE_COHORTS:
        await callback.answer("Список изменился — обновите его.", show_alert=True)
        return
    business_id = _uuid(parts[2])
    customer_id = _uuid(parts[3])
    cohort = _RETENTION_CODE_COHORTS[parts[4]]
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        prepared = await asyncio.to_thread(
            prepare_reactivation_sales_lead,
            actor=actor,
            customer_id=customer_id,
            expected_cohort=cohort,
        )
    except (
        RetentionCandidateUnavailable,
        SalesInvariantViolation,
        TenantAccessDenied,
        TenantPermissionDenied,
    ):
        await callback.answer(
            "Список изменился — обновите его перед действием.",
            show_alert=True,
        )
        return
    await state.clear()
    await callback.answer("Добавлено в работу")
    if prepared.route_platform is None:
        route_text = (
            "Безопасный канал для сообщения сейчас недоступен, поэтому это добавлено "
            "как ручная работа."
        )
    else:
        route_text = (
            "Можно продолжить через существующий канал клиента. Сообщение всё равно "
            "потребует отдельного подтверждения перед отправкой."
        )
    await control._callback_message(callback).answer(
        "✅ Клиент добавлен в Обращения.\n\n"
        f"{route_text}\n\n"
        "Клиенту сейчас ничего не отправлено.",
        reply_markup=control._keyboard(
            [
                [("🛠 Открыть обращения", f"cps:swm:{_token(business_id)}")],
                [("♻️ К списку возврата", f"cps:sr:{_token(business_id)}")],
            ]
        ),
    )


async def _send_sales_work(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    actor = await control._actor(user_id, business_id)
    items = await asyncio.to_thread(list_sales_work, actor=actor, limit=12)
    business_token = _token(business_id)
    rows: list[list[tuple[str, str]]] = []
    from clientplatform.application.sales_ai_drafts import (
        sales_ai_enabled_for_business,
        sales_ai_runtime_available,
    )

    runtime_ai_available = sales_ai_runtime_available()
    ai_available = (
        runtime_ai_available
        and await asyncio.to_thread(sales_ai_enabled_for_business, actor=actor)
    )
    if not items:
        text = (
            "💬 Обращения\n\nПока нет активных обращений. "
            "Когда клиент напишет или появится другой реальный сигнал интереса, "
            "ClientPlatform покажет здесь следующий шаг."
        )
    else:
        lines = ["💬 Обращения", ""]
        for index, item in enumerate(items, start=1):
            action = item.get("next_action_kind")
            action_text = (
                _ACTION_LABELS.get(str(action), "Проверить ситуацию")
                if action
                else "пока не определён"
            )
            lines.extend(
                [
                    f"{index}. {item.get('customer_name') or 'Клиент'}",
                    f"   Статус: {_STAGE_LABELS.get(str(item.get('stage')), 'В работе')}",
                    f"   Источник: {_source_label(item.get('source_kind'))}",
                    f"   Следующий шаг: {action_text}",
                ]
            )
            candidate = str(item.get("commercial_candidate_title") or "").strip()
            if candidate:
                lines.append(f"   Что можно предложить: {candidate}")
            plan_id = str(item.get("next_plan_id") or "")
            plan_status = str(item.get("next_plan_status") or "")
            if plan_status == "approved":
                lines.append("   Разрешение: ✅ отправка разрешена Вами")
            elif (
                plan_id
                and plan_status == "planned"
                and bool(item.get("next_plan_requires_approval"))
            ):
                rows.append(
                    [
                        (
                            f"✅ Одобрить шаг для {index}",
                            f"cps:swa:{business_token}:{_token(plan_id)}",
                        )
                    ]
                )
            if ai_available and plan_id and action not in {None, "human_handoff", "noop"}:
                rows.append(
                    [
                        (
                            f"🧠 Черновик ответа для {index}",
                            f"cps:sad:{business_token}:{_token(str(item['id']))}",
                        )
                    ]
                )
            lines.append("")
        text = "\n".join(lines).rstrip()
    if runtime_ai_available:
        rows.append(
            [
                (
                    "🧠 Выключить ИИ-помощника" if ai_available else "🧠 Подключить ИИ-помощника",
                    f"cps:sat:{business_token}",
                )
            ]
        )
    rows.append([("← Обращения и продажи", f"cps:s:{business_token}")])
    await message.answer(text, reply_markup=control._keyboard(rows))


@router.callback_query(F.data.startswith("cps:sw:"))
async def open_sales_work(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = _uuid(str(callback.data).split(":", 2)[2])
    await state.clear()
    await callback.answer()
    await _send_sales_work(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cps:swa:"))
async def approve_sales_plan(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, plan_id = _uuid(parts[2]), _uuid(parts[3])
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        authorization = await asyncio.to_thread(
            approve_and_authorize_sales_outbound,
            actor=actor,
            plan_id=plan_id,
        )
    except (PermissionError, ValueError, RuntimeError):
        await callback.answer(
            "Не удалось одобрить шаг: проверьте клиента и актуальность рекомендации.",
            show_alert=True,
        )
        return
    if not bool(authorization.get("dispatch_allowed")):
        await callback.answer("Отправка не разрешена", show_alert=True)
        return
    await state.clear()
    await callback.answer("Одобрено — отправка разрешена")
    await _send_sales_work(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cps:sat:"))
async def toggle_sales_ai(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = _uuid(str(callback.data).split(":", 2)[2])
    actor = await control._actor(int(callback.from_user.id), business_id)
    from clientplatform.application.sales_ai_drafts import (
        sales_ai_runtime_available,
        sales_ai_runtime_consent_target,
        sales_ai_runtime_provider_label,
    )
    from clientplatform.application.sales_ai_settings import (
        get_business_sales_ai_enabled,
        set_business_sales_ai_enabled,
    )

    if not sales_ai_runtime_available():
        await callback.answer("ИИ сейчас не настроен на сервере", show_alert=True)
        return
    enabled = await asyncio.to_thread(get_business_sales_ai_enabled, actor=actor)
    await state.clear()
    if enabled:
        try:
            await asyncio.to_thread(
                set_business_sales_ai_enabled, actor=actor, enabled=False
            )
        except (PermissionError, ValueError, RuntimeError):
            await callback.answer(
                "Недостаточно прав для изменения настроек ИИ", show_alert=True
            )
            return
        await callback.answer("ИИ-помощник выключен")
        await _send_sales_work(
            control._callback_message(callback),
            user_id=int(callback.from_user.id),
            business_id=business_id,
        )
        return
    await callback.answer()
    token = _token(business_id)
    provider_label = sales_ai_runtime_provider_label()
    consent_target = sales_ai_runtime_consent_target()
    await control._callback_message(callback).answer(
        "🧠 Подключить ИИ-помощника?\n\n"
        f"После включения тексты новых клиентских сообщений этого бизнеса будут "
        f"передаваться в {provider_label} ({consent_target}) для анализа и подготовки "
        "черновиков. ИИ не получает права отправлять сообщения и не подтверждает "
        "оплату или запись.\n\n"
        "Если администратор сменит AI-провайдера или домен API, это согласие "
        "автоматически перестанет действовать и ИИ потребуется включить заново.\n\n"
        "По умолчанию ClientPlatform удаляет из текста очевидные телефоны, e-mail, "
        "длинные номера и ссылки перед отправкой во внешний AI. Нажимая кнопку ниже, "
        "Вы также подтверждаете, что уведомили клиентов о применении внешнего AI к "
        "их сообщениям. Для чувствительных сфер используйте режим no_cloud или отдельную "
        "согласованную политику обработки данных.",
        reply_markup=control._keyboard(
            [
                [("✅ Включить с маскированием контактов", f"cps:sae:{token}")],
                [("✖️ Не включать", f"cps:sw:{token}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cps:sae:"))
async def enable_sales_ai(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = _uuid(str(callback.data).split(":", 2)[2])
    actor = await control._actor(int(callback.from_user.id), business_id)
    from clientplatform.application.sales_ai_drafts import sales_ai_runtime_available
    from clientplatform.application.sales_ai_settings import set_business_sales_ai_enabled

    if not sales_ai_runtime_available():
        await callback.answer("ИИ сейчас не настроен на сервере", show_alert=True)
        return
    try:
        await asyncio.to_thread(
            set_business_sales_ai_enabled,
            actor=actor,
            enabled=True,
            data_mode="redacted",
            customer_notice_confirmed=True,
        )
    except (PermissionError, ValueError, RuntimeError):
        await callback.answer(
            "Недостаточно прав для изменения настроек ИИ", show_alert=True
        )
        return
    await state.clear()
    await callback.answer("ИИ-помощник включён")
    await _send_sales_work(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cps:sad:"))
async def draft_sales_answer(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, lead_id = _uuid(parts[2]), _uuid(parts[3])
    actor = await control._actor(int(callback.from_user.id), business_id)
    await state.clear()
    await callback.answer("Готовлю черновик…")
    from clientplatform.application.sales_ai_drafts import draft_sales_reply
    from clientplatform.infrastructure.sales_ai_provider import SalesAIProviderError

    try:
        draft = await draft_sales_reply(actor=actor, lead_id=lead_id)
    except (SalesAIProviderError, ValueError, RuntimeError):
        await control._callback_message(callback).answer(
            "Не удалось подготовить актуальный черновик. Возможно, новое сообщение "
            "ещё анализируется или обращение требует человека.",
            reply_markup=_back_keyboard(business_id),
        )
        return
    await control._callback_message(callback).answer(
        "🧠 Черновик ИИ — проверьте перед отправкой\n\n"
        f"{draft.text}\n\n"
        "ClientPlatform ничего не отправил клиенту автоматически.",
        reply_markup=_back_keyboard(business_id),
    )


async def _send_handoffs(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    actor = await control._actor(user_id, business_id)
    items = await asyncio.to_thread(list_sales_handoff_work, actor=actor, limit=12)
    if not items:
        await message.answer(
            "🙋 Нужно подключиться\n\nСейчас нет обращений, где требуется Ваше личное участие.",
            reply_markup=_back_keyboard(business_id),
        )
        return

    rows: list[list[tuple[str, str]]] = []
    lines = ["🙋 Нужно подключиться", ""]
    business_token = _token(business_id)
    for index, item in enumerate(items, start=1):
        handoff_token = _token(str(item["id"]))
        status = "уже взято в работу" if str(item.get("status")) == "claimed" else "ожидает"
        lines.extend(
            [
                f"{index}. {item.get('customer_name') or 'Клиент'} · "
                f"{_SEVERITY_LABELS.get(str(item.get('severity')), '🟢 Обычно')}",
                f"   {_HANDOFF_REASON_LABELS.get(str(item.get('reason')), 'Нужно личное внимание')} · {status}",
                "",
            ]
        )
        if str(item.get("status")) == "open":
            rows.append([("✋ Взять", f"cps:shc:{business_token}:{handoff_token}")])
        rows.append([("✅ Готово", f"cps:shr:{business_token}:{handoff_token}")])
    rows.append([("← Обращения и продажи", f"cps:s:{business_token}")])
    await message.answer("\n".join(lines).rstrip(), reply_markup=control._keyboard(rows))


@router.callback_query(F.data.startswith("cps:sh:"))
async def open_sales_handoffs(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = _uuid(str(callback.data).split(":", 2)[2])
    await state.clear()
    await callback.answer()
    await _send_handoffs(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cps:shc:"))
async def claim_handoff(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, handoff_id = _uuid(parts[2]), _uuid(parts[3])
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        await asyncio.to_thread(claim_sales_handoff, actor=actor, handoff_id=handoff_id)
    except (PermissionError, ValueError, RuntimeError):
        await callback.answer(
            "Не удалось взять: возможно, обращение уже изменилось.",
            show_alert=True,
        )
        return
    await state.clear()
    await callback.answer("Взято в работу")
    await _send_handoffs(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cps:shr:"))
async def resolve_handoff(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, handoff_id = _uuid(parts[2]), _uuid(parts[3])
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        await asyncio.to_thread(resolve_sales_handoff, actor=actor, handoff_id=handoff_id)
    except (PermissionError, ValueError, RuntimeError):
        await callback.answer(
            "Не удалось закрыть: возможно, обращение уже изменилось.",
            show_alert=True,
        )
        return
    await state.clear()
    await callback.answer("Готово")
    await _send_handoffs(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cps:sf:"))
async def open_sales_funnel(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = _uuid(str(callback.data).split(":", 2)[2])
    await state.clear()
    actor = await control._actor(int(callback.from_user.id), business_id)
    snapshot = await asyncio.to_thread(get_sales_funnel_snapshot, actor=actor)
    total = snapshot.total
    lines = [
        "📊 Как идут продажи",
        "",
        "Только подтверждённые действия клиентов — без догадок.",
        "",
        f"Обращений: {total.discovered}",
        f"Вступили в диалог: {total.engaged} ({total.engagement_percent}%)",
        f"Подходят для предложения: {total.qualified} ({total.qualification_percent}%)",
        f"Перешли к оформлению: {total.checkout} ({total.checkout_percent}%)",
        f"Оплатили: {total.won} ({total.win_percent}% от всех обращений)",
        f"Не состоялось: {total.lost}",
        f"Требуют человека: {total.open_handoffs}",
    ]
    if snapshot.by_source:
        lines.extend(["", "По источникам:"])
        for source, counts in snapshot.by_source.items():
            lines.append(
                f"• {_source_label(source)}: {counts.discovered} обращ. · {counts.won} оплат"
            )
    await callback.answer()
    await control._callback_message(callback).answer(
        "\n".join(lines),
        reply_markup=_back_keyboard(business_id),
    )


async def _send_ladders(message: Message, *, user_id: int, business_id: str) -> None:
    actor = await control._actor(user_id, business_id)
    ladders = await asyncio.to_thread(list_commercial_ladders, actor=actor)
    business_token = _token(business_id)
    rows: list[list[tuple[str, str]]] = []
    lines = [
        "🧩 Что предлагать",
        "",
        "Здесь можно настроить, что предложить человеку сначала, что — потом, "
        "и когда переходить к основной услуге или сопровождению.",
        "ClientPlatform может подобрать подходящий вариант, но внешнее действие "
        "откроется только после Вашего подтверждения.",
        "",
    ]
    if not ladders:
        lines.append("Наборов предложений пока нет.")
    else:
        for item in ladders:
            lines.append(f"• {item['name']} · этапов: {int(item['step_count'])}")
            rows.append(
                [
                    (
                        f"🧩 {item['name']}",
                        f"cps:slv:{business_token}:{_token(str(item['id']))}",
                    )
                ]
            )
    rows.extend(
        [
            [("➕ Создать набор предложений", f"cps:sln:{business_token}")],
            [("← Обращения и продажи", f"cps:s:{business_token}")],
        ]
    )
    await message.answer("\n".join(lines), reply_markup=control._keyboard(rows))


@router.callback_query(F.data.startswith("cps:sl:"))
async def open_sales_ladders(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = _uuid(str(callback.data).split(":", 2)[2])
    await state.clear()
    await callback.answer()
    await _send_ladders(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


async def _send_ladder_detail(
    message: Message,
    *,
    user_id: int,
    business_id: str,
    ladder_id: str,
) -> None:
    actor = await control._actor(user_id, business_id)
    ladders, steps = await asyncio.gather(
        asyncio.to_thread(list_commercial_ladders, actor=actor),
        asyncio.to_thread(
            list_commercial_ladder_steps,
            actor=actor,
            ladder_id=ladder_id,
        ),
    )
    selected = next((item for item in ladders if str(item["id"]) == ladder_id), None)
    if selected is None:
        raise ValueError("commercial ladder is not available")
    lines = [f"🧩 {selected['name']}", ""]
    if not steps:
        lines.append("Этапов пока нет.")
    else:
        for index, step in enumerate(steps, start=1):
            kind = _LADDER_KIND_LABELS.get(str(step.get("kind")), "Этап")
            approval = (
                " · с Вашим подтверждением"
                if bool(step.get("requires_human_approval"))
                else ""
            )
            lines.append(f"{index}. {step['title']} — {kind}{approval}")
    business_token, ladder_token = _token(business_id), _token(ladder_id)
    await message.answer(
        "\n".join(lines),
        reply_markup=control._keyboard(
            [
                [("➕ Добавить этап", f"cps:sla:{business_token}:{ladder_token}")],
                [("← Все предложения", f"cps:sl:{business_token}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cps:slv:"))
async def open_ladder_detail(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, ladder_id = _uuid(parts[2]), _uuid(parts[3])
    await state.clear()
    await callback.answer()
    try:
        await _send_ladder_detail(
            control._callback_message(callback),
            user_id=int(callback.from_user.id),
            business_id=business_id,
            ladder_id=ladder_id,
        )
    except ValueError:
        await control._callback_message(callback).answer(
            "Этот набор предложений больше не доступен.",
            reply_markup=_back_keyboard(business_id),
        )


@router.callback_query(F.data.startswith("cps:sln:"))
async def begin_ladder_creation(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = _uuid(str(callback.data).split(":", 2)[2])
    await control._actor(int(callback.from_user.id), business_id)
    await state.clear()
    await state.update_data(business_id=business_id)
    await state.set_state(ClientPlatformSalesUiState.ladder_name)
    await callback.answer()
    await control._callback_message(callback).answer(
        "Как назвать набор предложений?\n\nНапример: «Основной путь клиента».",
        reply_markup=control._keyboard(
            [[("✖️ Отмена", f"cps:sl:{_token(business_id)}")]]
        ),
    )


@router.message(ClientPlatformSalesUiState.ladder_name)
async def receive_ladder_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("business_id") or "")
    user_id = control._user_id(message)
    actor = await control._actor(user_id, business_id)
    name = " ".join(str(message.text or "").split())
    try:
        ladder_id = await asyncio.to_thread(
            create_commercial_ladder,
            actor=actor,
            name=name,
        )
    except ValueError:
        await message.answer("Нужно короткое понятное название — от 1 до 160 символов.")
        return
    await state.clear()
    await message.answer("✅ Набор предложений создан. Теперь добавьте первый этап.")
    await _send_ladder_detail(
        message,
        user_id=user_id,
        business_id=business_id,
        ladder_id=ladder_id,
    )


@router.callback_query(F.data.startswith("cps:sla:"))
async def begin_ladder_step(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, ladder_id = _uuid(parts[2]), _uuid(parts[3])
    actor = await control._actor(int(callback.from_user.id), business_id)
    await asyncio.to_thread(
        list_commercial_ladder_steps,
        actor=actor,
        ladder_id=ladder_id,
    )
    await state.clear()
    await state.update_data(business_id=business_id, ladder_id=ladder_id)
    await callback.answer()
    business_token, ladder_token = _token(business_id), _token(ladder_id)
    await control._callback_message(callback).answer(
        "Какой это этап?",
        reply_markup=control._keyboard(
            [
                [("🔎 Диагностика", f"cps:slk:{business_token}:{ladder_token}:d")],
                [("📋 Разбор / аудит", f"cps:slk:{business_token}:{ladder_token}:a")],
                [("🎯 Основная услуга", f"cps:slk:{business_token}:{ladder_token}:i")],
                [("🔁 Сопровождение", f"cps:slk:{business_token}:{ladder_token}:r")],
                [("✖️ Отмена", f"cps:slv:{business_token}:{ladder_token}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cps:slk:"))
async def choose_ladder_step_kind(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, ladder_id = _uuid(parts[2]), _uuid(parts[3])
    kind = _KIND_CODES.get(parts[4])
    if kind is None:
        await callback.answer("Неизвестный тип этапа", show_alert=True)
        return
    actor = await control._actor(int(callback.from_user.id), business_id)
    await asyncio.to_thread(
        list_commercial_ladder_steps,
        actor=actor,
        ladder_id=ladder_id,
    )
    await state.clear()
    await state.update_data(business_id=business_id, ladder_id=ladder_id, kind=kind)
    await state.set_state(ClientPlatformSalesUiState.ladder_step_title)
    await callback.answer()
    await control._callback_message(callback).answer(
        f"Как назвать этап «{_LADDER_KIND_LABELS[kind]}»?\n\n"
        "Например: «Первая консультация» или «Ежемесячное сопровождение».\n\n"
        "Безопасный режим включён: дальнейшее внешнее действие потребует Вашего подтверждения.",
        reply_markup=control._keyboard(
            [[("✖️ Отмена", f"cps:slv:{_token(business_id)}:{_token(ladder_id)}")]]
        ),
    )


@router.message(ClientPlatformSalesUiState.ladder_step_title)
async def receive_ladder_step_title(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("business_id") or "")
    ladder_id = str(data.get("ladder_id") or "")
    kind = str(data.get("kind") or "")
    user_id = control._user_id(message)
    actor = await control._actor(user_id, business_id)
    steps = await asyncio.to_thread(
        list_commercial_ladder_steps,
        actor=actor,
        ladder_id=ladder_id,
    )
    position = max((int(item["position"]) for item in steps), default=-1) + 1
    title = " ".join(str(message.text or "").split())
    try:
        await asyncio.to_thread(
            add_commercial_ladder_step,
            actor=actor,
            ladder_id=ladder_id,
            position=position,
            kind=kind,
            title=title,
            min_evidence_score=0.0,
            requires_human_approval=True,
        )
    except ValueError:
        await message.answer("Нужно короткое понятное название этапа — от 1 до 200 символов.")
        return
    await state.clear()
    await message.answer("✅ Этап добавлен.")
    await _send_ladder_detail(
        message,
        user_id=user_id,
        business_id=business_id,
        ladder_id=ladder_id,
    )


__all__ = ["ClientPlatformSalesUiState", "router", "send_sales_home"]

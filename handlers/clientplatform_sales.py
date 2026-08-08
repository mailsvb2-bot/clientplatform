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
from clientplatform.application.sales_metrics import get_sales_funnel_snapshot
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
    "website": "Сайт",
    "referral": "Рекомендации",
    "manual": "Вручную",
}


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
            [("📋 В работе", f"cps:sw:{token}"), ("🙋 Нужен человек", f"cps:sh:{token}")],
            [("📊 Воронка", f"cps:sf:{token}"), ("🪜 Линейка", f"cps:sl:{token}")],
            [("🏠 В кабинет", f"cp:business:{token}")],
        ]
    )


def _back_keyboard(business_id: str):
    return control._keyboard([[('← Продажи', f"cps:s:{_token(business_id)}")]])


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
        "💼 Продажи\n\n"
        "Здесь собраны реальные обращения и подтверждённые результаты. "
        "ClientPlatform ничего не отправляет клиентам сам: внешнее действие остаётся за Вами.\n\n"
        f"Сейчас в работе: {active_count}\n"
        f"Нужен человек: {len(handoffs)}\n"
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


@router.callback_query(F.data.startswith("cps:sw:"))
async def open_sales_work(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = _uuid(str(callback.data).split(":", 2)[2])
    await state.clear()
    actor = await control._actor(int(callback.from_user.id), business_id)
    items = await asyncio.to_thread(list_sales_work, actor=actor, limit=12)
    await callback.answer()
    if not items:
        text = (
            "📋 В работе\n\nПока нет активных обращений. "
            "Когда появятся реальные сигналы от клиентов, они будут собраны здесь."
        )
    else:
        lines = ["📋 В работе", ""]
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
                    "",
                ]
            )
        text = "\n".join(lines).rstrip()
    await control._callback_message(callback).answer(
        text,
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
            "🙋 Нужен человек\n\nСейчас нет обращений, где требуется Ваше личное участие.",
            reply_markup=_back_keyboard(business_id),
        )
        return

    rows: list[list[tuple[str, str]]] = []
    lines = ["🙋 Нужен человек", ""]
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
    rows.append([("← Продажи", f"cps:s:{business_token}")])
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
        "📊 Воронка",
        "",
        "Только подтверждённые данные — без придуманных AI-событий.",
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
        "🪜 Линейка",
        "",
        "Настройте путь клиента от первого шага к основной услуге и сопровождению.",
        "Никакое предложение не отправляется автоматически.",
        "",
    ]
    if not ladders:
        lines.append("Линеек пока нет.")
    else:
        for item in ladders:
            lines.append(f"• {item['name']} · этапов: {int(item['step_count'])}")
            rows.append(
                [
                    (
                        f"🪜 {item['name']}",
                        f"cps:slv:{business_token}:{_token(str(item['id']))}",
                    )
                ]
            )
    rows.extend(
        [
            [("➕ Создать линейку", f"cps:sln:{business_token}")],
            [("← Продажи", f"cps:s:{business_token}")],
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
    lines = [f"🪜 {selected['name']}", ""]
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
                [("← Все линейки", f"cps:sl:{business_token}")],
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
            "Эта линейка больше не доступна.",
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
        "Как назвать линейку?\n\nНапример: «Основной путь клиента».",
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
    await message.answer("✅ Линейка создана. Теперь добавьте первый этап.")
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

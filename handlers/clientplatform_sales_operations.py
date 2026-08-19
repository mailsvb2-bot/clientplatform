from __future__ import annotations

"""Owner mutation surface for the canonical tenant-scoped sales domain.

This module is presentation only. Business invariants and persistence remain in
``clientplatform.application.sales_operations`` and ``SalesRepository``.
"""

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.sales_operations import (
    add_sales_note,
    assign_sales_lead,
    clear_sales_next_action,
    set_sales_next_action,
    transition_sales_lead,
    unassign_sales_lead,
)
from clientplatform.application.sales_ui import (
    list_recent_closed_sales_work,
    list_sales_work,
)
from clientplatform.domain.sales import SalesError, SalesLeadStage

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_sales_operations")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())


class ClientPlatformSalesOperationsState(StatesGroup):
    next_action = State()
    close_reason = State()
    note = State()


_STAGE_LABELS = {
    "new": "Новый интерес",
    "contacted": "На связи",
    "qualified": "Готов к предложению",
    "checkout": "Оформление",
    "won": "Оплатил / выиграно",
    "lost": "Потеряно",
}
_SOURCE_LABELS = {
    "organic": "Органика",
    "referral": "Рекомендация",
    "telegram": "Telegram",
    "vk": "VK",
    "max": "MAX",
    "website": "Сайт",
    "yandex_direct": "Яндекс Директ",
    "partner": "Партнёр",
    "manual": "Вручную",
    "unknown": "Не определён",
}
_STAGE_CODES = {
    "c": SalesLeadStage.CONTACTED,
    "q": SalesLeadStage.QUALIFIED,
    "k": SalesLeadStage.CHECKOUT,
}
_CLOSE_CODES = {
    "w": SalesLeadStage.WON,
    "l": SalesLeadStage.LOST,
}
_DUE_HOURS = {"1": 1, "24": 24, "72": 72, "168": 168}


class _SalesModule(Protocol):
    _home_keyboard: Any
    control: Any
    _sales_operations_installed: bool


def _token(value: str) -> str:
    return control._uuid_token(value)


def _uuid(value: str) -> str:
    return control._token_uuid(value)


def _short_time(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "без срока"
    return raw.replace("T", " ").replace("+00:00", " UTC")[:22]


def _source_label(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return _SOURCE_LABELS.get(normalized, normalized or "Другой источник")


def _assignee_label(item: dict[str, Any], *, user_id: int) -> str:
    member_id = str(item.get("assigned_member_id") or "").strip()
    if not member_id:
        return "не назначен"
    assigned_user_id = item.get("assigned_user_id")
    if assigned_user_id is not None and int(assigned_user_id) == int(user_id):
        return "Вы"
    return "другой участник команды"


def _attribution_line(item: dict[str, Any]) -> str:
    source = _source_label(item.get("attribution_source") or item.get("source_kind"))
    ref_type = str(item.get("attribution_source_ref_type") or "").strip()
    ref_id = str(item.get("attribution_source_ref_id") or "").strip()
    campaign_id = str(item.get("attribution_promotion_campaign_id") or "").strip()
    details: list[str] = []
    if ref_type and ref_id:
        details.append(f"{ref_type}: {ref_id}")
    elif ref_id:
        details.append(ref_id)
    if campaign_id:
        details.append(f"кампания: {campaign_id}")
    return source if not details else f"{source} · {' · '.join(details)}"


def _item_text(item: dict[str, Any], *, user_id: int) -> str:
    next_action = str(item.get("next_action") or "").strip() or "не задано"
    closure_reason = str(item.get("closure_reason") or "").strip()
    source_ref = str(item.get("source_ref") or "").strip()
    lines = [
        f"👤 {item.get('customer_name') or 'Клиент'}",
        f"Статус: {_STAGE_LABELS.get(str(item.get('stage')), str(item.get('stage') or '—'))}",
        f"Ответственный: {_assignee_label(item, user_id=user_id)}",
        f"Следующее действие: {next_action}",
        f"Срок: {_short_time(item.get('due_at'))}",
        f"Источник лида: {_source_label(item.get('source_kind'))}",
    ]
    if source_ref:
        lines.append(f"Метка источника: {source_ref}")
    lines.append(f"Атрибуция: {_attribution_line(item)}")
    if closure_reason:
        lines.append(f"Причина закрытия: {closure_reason}")
    return "\n".join(lines)


def _find_item(
    open_items: list[dict[str, Any]],
    closed_items: list[dict[str, Any]],
    lead_id: str,
) -> dict[str, Any] | None:
    for item in [*open_items, *closed_items]:
        if str(item.get("id") or "") == lead_id:
            return item
    return None


async def _load_item(*, actor: Any, lead_id: str) -> dict[str, Any] | None:
    open_items, closed_items = await asyncio.gather(
        asyncio.to_thread(list_sales_work, actor=actor, limit=50),
        asyncio.to_thread(list_recent_closed_sales_work, actor=actor, limit=50),
    )
    return _find_item(open_items, closed_items, lead_id)


def _detail_keyboard(
    business_id: str,
    item: dict[str, Any],
    *,
    user_id: int,
) -> InlineKeyboardMarkup:
    business_token = _token(business_id)
    lead_token = _token(str(item["id"]))
    stage = str(item.get("stage") or "")
    rows: list[list[tuple[str, str]]] = []
    if stage not in {"won", "lost"}:
        if not item.get("assigned_member_id") or item.get("assigned_user_id") != user_id:
            rows.append([("🙋 Назначить меня", f"cps:swme:{business_token}:{lead_token}")])
        else:
            rows.append([("Снять ответственного", f"cps:swmu:{business_token}:{lead_token}")])
        rows.append(
            [
                ("📌 Следующее действие", f"cps:swmn:{business_token}:{lead_token}"),
                ("📝 Заметка", f"cps:swmo:{business_token}:{lead_token}"),
            ]
        )
        if item.get("next_action"):
            rows.append(
                [
                    ("⏱ +1 час", f"cps:swmd:{business_token}:{lead_token}:1"),
                    ("🌅 Завтра", f"cps:swmd:{business_token}:{lead_token}:24"),
                ]
            )
            rows.append(
                [
                    ("📅 +3 дня", f"cps:swmd:{business_token}:{lead_token}:72"),
                    ("Без срока", f"cps:swmd:{business_token}:{lead_token}:n"),
                ]
            )
            rows.append([("Очистить следующий шаг", f"cps:swmx:{business_token}:{lead_token}")])
        rows.append(
            [
                ("На связи", f"cps:swms:{business_token}:{lead_token}:c"),
                ("Квалифицирован", f"cps:swms:{business_token}:{lead_token}:q"),
            ]
        )
        rows.append(
            [
                ("Оформление", f"cps:swms:{business_token}:{lead_token}:k"),
                ("✅ Выиграно", f"cps:swmc:{business_token}:{lead_token}:w"),
                ("❌ Потеряно", f"cps:swmc:{business_token}:{lead_token}:l"),
            ]
        )
    else:
        rows.append([("📝 Заметка", f"cps:swmo:{business_token}:{lead_token}")])
        if stage == "lost":
            rows.append([("↩️ Вернуть в работу", f"cps:swmr:{business_token}:{lead_token}")])
    rows.append([("← Управление обращениями", f"cps:swm:{business_token}")])
    return control._keyboard(rows)


async def _send_detail(
    message: Message,
    *,
    user_id: int,
    business_id: str,
    lead_id: str,
) -> None:
    actor = await control._actor(user_id, business_id)
    item = await _load_item(actor=actor, lead_id=lead_id)
    if item is None:
        await message.answer(
            "Карточка уже изменилась или больше недоступна в этом бизнесе.",
            reply_markup=control._keyboard(
                [[("← Управление обращениями", f"cps:swm:{_token(business_id)}")]]
            ),
        )
        return
    await message.answer(
        _item_text(item, user_id=user_id),
        reply_markup=_detail_keyboard(business_id, item, user_id=user_id),
    )


async def _send_manage_work(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    actor = await control._actor(user_id, business_id)
    items = await asyncio.to_thread(list_sales_work, actor=actor, limit=12)
    business_token = _token(business_id)
    rows: list[list[tuple[str, str]]] = []
    if not items:
        text = "🛠 Управление обращениями\n\nАктивных обращений сейчас нет."
    else:
        lines = ["🛠 Управление обращениями", ""]
        for index, item in enumerate(items, start=1):
            lines.extend(
                [
                    f"{index}. {item.get('customer_name') or 'Клиент'}",
                    f"   {_STAGE_LABELS.get(str(item.get('stage')), 'В работе')} · {_assignee_label(item, user_id=user_id)}",
                    f"   Следующий шаг: {str(item.get('next_action') or '').strip() or 'не задан'}",
                    f"   Срок: {_short_time(item.get('due_at'))}",
                    f"   Источник: {_attribution_line(item)}",
                    "",
                ]
            )
            rows.append(
                [
                    (
                        f"Управлять: {index}",
                        f"cps:swv:{business_token}:{_token(str(item['id']))}",
                    )
                ]
            )
        text = "\n".join(lines).rstrip()
    rows.extend(
        [
            [("📁 Недавно закрытые", f"cps:swc:{business_token}")],
            [("🧠 Рекомендации и ИИ", f"cps:sw:{business_token}")],
            [("← Обращения и продажи", f"cps:s:{business_token}")],
        ]
    )
    await message.answer(text, reply_markup=control._keyboard(rows))


async def _send_closed_work(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    actor = await control._actor(user_id, business_id)
    items = await asyncio.to_thread(list_recent_closed_sales_work, actor=actor, limit=12)
    business_token = _token(business_id)
    rows: list[list[tuple[str, str]]] = []
    if not items:
        text = "📁 Недавно закрытые\n\nЗакрытых обращений пока нет."
    else:
        lines = ["📁 Недавно закрытые", ""]
        for index, item in enumerate(items, start=1):
            reason = str(item.get("closure_reason") or "").strip() or "причина не указана"
            lines.extend(
                [
                    f"{index}. {item.get('customer_name') or 'Клиент'}",
                    f"   {_STAGE_LABELS.get(str(item.get('stage')), 'Закрыто')} · {reason}",
                    f"   Источник: {_attribution_line(item)}",
                    "",
                ]
            )
            rows.append(
                [
                    (
                        f"Открыть: {index}",
                        f"cps:swv:{business_token}:{_token(str(item['id']))}",
                    )
                ]
            )
        text = "\n".join(lines).rstrip()
    rows.append([("← Управление обращениями", f"cps:swm:{business_token}")])
    await message.answer(text, reply_markup=control._keyboard(rows))


def install_sales_operations(sales_module: _SalesModule) -> None:
    """Expose mutations from the existing sales home without replacing its brain."""

    if bool(getattr(sales_module, "_sales_operations_installed", False)):
        return
    original_keyboard = sales_module._home_keyboard

    def operations_keyboard(business_id: str) -> InlineKeyboardMarkup:
        current = original_keyboard(business_id)
        rows = [list(row) for row in current.inline_keyboard]
        rows.insert(
            max(0, len(rows) - 1),
            [
                InlineKeyboardButton(
                    text="🛠 Управлять обращениями",
                    callback_data=f"cps:swm:{_token(business_id)}",
                )
            ],
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    sales_module._home_keyboard = operations_keyboard
    sales_module._sales_operations_installed = True


@router.callback_query(F.data.startswith("cps:swm:"))
async def open_sales_operations(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = _uuid(str(callback.data).split(":", 2)[2])
    await state.clear()
    await callback.answer()
    await _send_manage_work(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cps:swc:"))
async def open_closed_sales(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = _uuid(str(callback.data).split(":", 2)[2])
    await state.clear()
    await callback.answer()
    await _send_closed_work(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cps:swv:"))
async def open_sales_lead(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, lead_id = _uuid(parts[2]), _uuid(parts[3])
    await state.clear()
    await callback.answer()
    await _send_detail(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        lead_id=lead_id,
    )


@router.callback_query(F.data.startswith("cps:swme:"))
async def assign_sales_lead_to_self(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, lead_id = _uuid(parts[2]), _uuid(parts[3])
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        await asyncio.to_thread(
            assign_sales_lead,
            actor=actor,
            lead_id=lead_id,
            member_id=actor.membership_id,
        )
    except (SalesError, PermissionError, ValueError):
        await callback.answer("Не удалось назначить ответственного.", show_alert=True)
        return
    await state.clear()
    await callback.answer("Обращение назначено Вам")
    await _send_detail(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        lead_id=lead_id,
    )


@router.callback_query(F.data.startswith("cps:swmu:"))
async def unassign_sales_lead_owner(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, lead_id = _uuid(parts[2]), _uuid(parts[3])
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        await asyncio.to_thread(unassign_sales_lead, actor=actor, lead_id=lead_id)
    except (SalesError, PermissionError, ValueError):
        await callback.answer("Не удалось снять ответственного.", show_alert=True)
        return
    await state.clear()
    await callback.answer("Ответственный снят")
    await _send_detail(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        lead_id=lead_id,
    )


@router.callback_query(F.data.startswith("cps:swmn:"))
async def begin_sales_next_action(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, lead_id = _uuid(parts[2]), _uuid(parts[3])
    await state.set_state(ClientPlatformSalesOperationsState.next_action)
    await state.update_data(sales_business_id=business_id, sales_lead_id=lead_id)
    await callback.answer()
    await control._callback_message(callback).answer(
        "Напишите следующим сообщением конкретное следующее действие по этому клиенту. "
        "После сохранения срок можно выбрать кнопкой в карточке."
    )


@router.message(ClientPlatformSalesOperationsState.next_action)
async def capture_sales_next_action(message: Message, state: FSMContext) -> None:
    action = str(message.text or "").strip()
    if not action:
        await message.answer("Следующее действие не может быть пустым. Напишите его текстом.")
        return
    if len(action) > 500:
        await message.answer("Сократите следующее действие до 500 символов.")
        return
    data = await state.get_data()
    business_id = str(data.get("sales_business_id") or "")
    lead_id = str(data.get("sales_lead_id") or "")
    if not business_id or not lead_id:
        await state.clear()
        await message.answer("Карточка устарела. Откройте обращения заново.")
        return
    actor = await control._actor(int(message.from_user.id), business_id)
    try:
        await asyncio.to_thread(
            set_sales_next_action,
            actor=actor,
            lead_id=lead_id,
            next_action=action,
            due_at=None,
        )
    except (SalesError, PermissionError, ValueError):
        await state.clear()
        await message.answer("Не удалось сохранить следующий шаг. Откройте карточку и попробуйте ещё раз.")
        return
    await state.clear()
    await message.answer("Следующий шаг сохранён. При необходимости выберите срок в карточке.")
    await _send_detail(
        message,
        user_id=int(message.from_user.id),
        business_id=business_id,
        lead_id=lead_id,
    )


@router.callback_query(F.data.startswith("cps:swmd:"))
async def set_sales_due_owner(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, lead_id, due_code = _uuid(parts[2]), _uuid(parts[3]), parts[4]
    actor = await control._actor(int(callback.from_user.id), business_id)
    item = await _load_item(actor=actor, lead_id=lead_id)
    next_action = "" if item is None else str(item.get("next_action") or "").strip()
    if not next_action or item is None or str(item.get("stage")) in {"won", "lost"}:
        await callback.answer("Следующий шаг уже изменился. Обновите карточку.", show_alert=True)
        return
    due_at = None
    if due_code != "n":
        hours = _DUE_HOURS.get(due_code)
        if hours is None:
            await callback.answer("Неизвестный срок.", show_alert=True)
            return
        due_at = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(
            timespec="seconds"
        )
    try:
        await asyncio.to_thread(
            set_sales_next_action,
            actor=actor,
            lead_id=lead_id,
            next_action=next_action,
            due_at=due_at,
        )
    except (SalesError, PermissionError, ValueError):
        await callback.answer("Не удалось сохранить срок. Обновите карточку.", show_alert=True)
        return
    await state.clear()
    await callback.answer("Срок обновлён")
    await _send_detail(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        lead_id=lead_id,
    )


@router.callback_query(F.data.startswith("cps:swmx:"))
async def clear_sales_next_action_owner(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, lead_id = _uuid(parts[2]), _uuid(parts[3])
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        await asyncio.to_thread(clear_sales_next_action, actor=actor, lead_id=lead_id)
    except (SalesError, PermissionError, ValueError):
        await callback.answer("Не удалось очистить следующий шаг.", show_alert=True)
        return
    await state.clear()
    await callback.answer("Следующий шаг очищен")
    await _send_detail(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        lead_id=lead_id,
    )


@router.callback_query(F.data.startswith("cps:swmo:"))
async def begin_sales_note(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, lead_id = _uuid(parts[2]), _uuid(parts[3])
    await state.set_state(ClientPlatformSalesOperationsState.note)
    await state.update_data(sales_business_id=business_id, sales_lead_id=lead_id)
    await callback.answer()
    await control._callback_message(callback).answer(
        "Напишите заметку по клиенту следующим сообщением."
    )


@router.message(ClientPlatformSalesOperationsState.note)
async def capture_sales_note(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("sales_business_id") or "")
    lead_id = str(data.get("sales_lead_id") or "")
    if not business_id or not lead_id:
        await state.clear()
        await message.answer("Карточка устарела. Откройте обращения заново.")
        return
    note = str(message.text or "").strip()
    if not note:
        await message.answer("Заметка не может быть пустой.")
        return
    actor = await control._actor(int(message.from_user.id), business_id)
    try:
        await asyncio.to_thread(
            add_sales_note,
            actor=actor,
            lead_id=lead_id,
            note=note,
            dedupe_key=f"telegram:{message.chat.id}:{message.message_id}",
        )
    except (SalesError, PermissionError, ValueError):
        await message.answer("Не удалось сохранить заметку. Откройте карточку и попробуйте ещё раз.")
        return
    await state.clear()
    await message.answer("Заметка сохранена.")
    await _send_detail(
        message,
        user_id=int(message.from_user.id),
        business_id=business_id,
        lead_id=lead_id,
    )


@router.callback_query(F.data.startswith("cps:swms:"))
async def set_sales_stage_owner(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, lead_id, stage_code = _uuid(parts[2]), _uuid(parts[3]), parts[4]
    stage = _STAGE_CODES.get(stage_code)
    if stage is None:
        await callback.answer("Неизвестный статус.", show_alert=True)
        return
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        await asyncio.to_thread(
            transition_sales_lead,
            actor=actor,
            lead_id=lead_id,
            stage=stage,
        )
    except (SalesError, PermissionError, ValueError):
        await callback.answer(
            "Карточка уже изменилась или этот переход сейчас нельзя выполнить.",
            show_alert=True,
        )
        return
    await state.clear()
    await callback.answer("Статус обновлён")
    await _send_detail(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        lead_id=lead_id,
    )


@router.callback_query(F.data.startswith("cps:swmc:"))
async def begin_close_sales_lead(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, lead_id, close_code = _uuid(parts[2]), _uuid(parts[3]), parts[4]
    stage = _CLOSE_CODES.get(close_code)
    if stage is None:
        await callback.answer("Неизвестный результат.", show_alert=True)
        return
    await state.set_state(ClientPlatformSalesOperationsState.close_reason)
    await state.update_data(
        sales_business_id=business_id,
        sales_lead_id=lead_id,
        sales_close_stage=stage.value,
    )
    await callback.answer()
    await control._callback_message(callback).answer(
        "Коротко укажите причину результата. Она сохранится в истории обращения."
    )


@router.message(ClientPlatformSalesOperationsState.close_reason)
async def capture_close_reason(message: Message, state: FSMContext) -> None:
    reason = str(message.text or "").strip()
    if not reason:
        await message.answer("Причина не может быть пустой. Напишите её одним сообщением.")
        return
    data = await state.get_data()
    business_id = str(data.get("sales_business_id") or "")
    lead_id = str(data.get("sales_lead_id") or "")
    stage_raw = str(data.get("sales_close_stage") or "")
    if not business_id or not lead_id or stage_raw not in {"won", "lost"}:
        await state.clear()
        await message.answer("Карточка устарела. Откройте обращения заново.")
        return
    actor = await control._actor(int(message.from_user.id), business_id)
    try:
        await asyncio.to_thread(
            transition_sales_lead,
            actor=actor,
            lead_id=lead_id,
            stage=SalesLeadStage(stage_raw),
            reason=reason,
        )
    except (SalesError, PermissionError, ValueError):
        await state.clear()
        await message.answer(
            "Карточка уже изменилась или этот результат сейчас нельзя сохранить. "
            "Откройте обращения заново."
        )
        return
    await state.clear()
    await message.answer("Результат сохранён.")
    await _send_detail(
        message,
        user_id=int(message.from_user.id),
        business_id=business_id,
        lead_id=lead_id,
    )


@router.callback_query(F.data.startswith("cps:swmr:"))
async def reopen_lost_sales_lead(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data).split(":")
    business_id, lead_id = _uuid(parts[2]), _uuid(parts[3])
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        await asyncio.to_thread(
            transition_sales_lead,
            actor=actor,
            lead_id=lead_id,
            stage=SalesLeadStage.NEW,
            reason="reopened_by_owner",
        )
    except (SalesError, PermissionError, ValueError):
        await callback.answer(
            "Карточка уже изменилась или её нельзя вернуть в работу.",
            show_alert=True,
        )
        return
    await state.clear()
    await callback.answer("Обращение возвращено в работу")
    await _send_detail(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        lead_id=lead_id,
    )


__all__ = ["install_sales_operations", "router"]

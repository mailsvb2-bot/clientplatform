from __future__ import annotations

import asyncio
import importlib

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from clientplatform.application.activity_directions import (
    archive_activity_direction,
    create_activity_direction,
    get_activity_direction,
    list_activity_direction_bindings,
    list_activity_directions,
    restore_activity_direction,
)
from clientplatform.domain.activity_directions import ActivityDirectionStatus

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_activity_directions")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())


class ClientPlatformActivityDirectionState(StatesGroup):
    title = State()
    description = State()


def _open_callback(business_id: str, direction_id: str) -> str:
    return (
        f"cp:diropen:{control._uuid_token(business_id)}:"
        f"{control._uuid_token(direction_id)}"
    )


async def _render_directions(
    message: Message,
    *,
    user_id: int,
    business_id: str,
    include_archived: bool = False,
) -> None:
    actor = await control._actor(user_id, business_id)
    directions = await asyncio.to_thread(
        list_activity_directions,
        actor=actor,
        include_archived=include_archived,
    )
    token = control._uuid_token(business_id)
    rows: list[list[tuple[str, str]]] = [
        [("➕ Добавить направление", f"cp:diradd:{token}")]
    ]
    for direction in directions:
        marker = "🧭" if direction.status == ActivityDirectionStatus.ACTIVE else "📦"
        rows.append(
            [
                (
                    f"{marker} {direction.title[:38]}",
                    _open_callback(business_id, direction.id),
                )
            ]
        )
    if include_archived:
        rows.append([("Скрыть архив", f"cp:dirs:{token}")])
    else:
        rows.append([("Показать архив", f"cp:dirarch:{token}")])
    rows.append([("⬅️ К настройкам", f"cpo:settings:{token}")])

    active_count = sum(
        1 for item in directions if item.status == ActivityDirectionStatus.ACTIVE
    )
    await message.answer(
        "🧭 Направления деятельности\n\n"
        "Здесь можно разделить работу организации на самостоятельные направления. "
        "Каждое направление остаётся частью одной организации: общий бренд, "
        "сотрудники, клиенты и каналы связи не дублируются.\n\n"
        f"Активных направлений: {active_count}.",
        reply_markup=control._keyboard(rows),
    )


@router.callback_query(F.data.startswith("cp:dirs:"))
async def open_activity_directions(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = control._token_uuid(str(callback.data).split(":", 2)[2])
    await state.clear()
    await callback.answer()
    await _render_directions(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cp:dirarch:"))
async def open_archived_activity_directions(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    business_id = control._token_uuid(str(callback.data).split(":", 2)[2])
    await state.clear()
    await callback.answer()
    await _render_directions(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        include_archived=True,
    )


@router.callback_query(F.data.startswith("cp:diradd:"))
async def begin_activity_direction(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = control._token_uuid(str(callback.data).split(":", 2)[2])
    actor = await control._actor(int(callback.from_user.id), business_id)
    actor.assert_can_manage_business()
    await state.clear()
    await state.update_data(activity_direction_business_id=business_id)
    await state.set_state(ClientPlatformActivityDirectionState.title)
    await callback.answer()
    await control._callback_message(callback).answer(
        "Как называется направление деятельности?\n\n"
        "Например: «Работа с частными клиентами», «Корпоративное направление», "
        "«Региональные проекты» или любое Ваше название."
    )


@router.message(ClientPlatformActivityDirectionState.title)
async def capture_activity_direction_title(message: Message, state: FSMContext) -> None:
    title = " ".join(str(message.text or "").split()).strip()
    if not title or len(title) > 160:
        await message.answer("Название должно содержать от 1 до 160 символов.")
        return
    await state.update_data(activity_direction_title=title)
    await state.set_state(ClientPlatformActivityDirectionState.description)
    await message.answer(
        "Коротко опишите это направление: что в него входит и для кого оно предназначено."
    )


@router.message(ClientPlatformActivityDirectionState.description)
async def capture_activity_direction_description(
    message: Message,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    business_id = str(data.get("activity_direction_business_id") or "").strip()
    title = str(data.get("activity_direction_title") or "").strip()
    description = " ".join(str(message.text or "").split()).strip()
    if not business_id or not title:
        await state.clear()
        await message.answer(
            "Создание направления было закрыто. Откройте «Направления деятельности» заново."
        )
        return
    if not description or len(description) > 2000:
        await message.answer("Описание должно содержать от 1 до 2000 символов.")
        return
    actor = await control._actor(control._user_id(message), business_id)
    try:
        direction = await asyncio.to_thread(
            create_activity_direction,
            actor=actor,
            title=title,
            description=description,
        )
    except Exception as exc:
        if "title already exists" not in str(exc):
            raise
        await message.answer(
            "Направление с таким названием уже есть. Укажите другое название."
        )
        return
    await state.clear()
    await message.answer(
        f"✅ Направление «{direction.title}» создано.\n\n"
        "Оно относится к этой же организации и использует её общий бренд, "
        "клиентскую базу, сотрудников и подключённые каналы."
    )
    await _render_directions(
        message,
        user_id=control._user_id(message),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cp:diropen:"))
async def open_activity_direction(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, direction_token = str(callback.data).split(":", 3)
    business_id = control._token_uuid(business_token)
    direction_id = control._token_uuid(direction_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    direction = await asyncio.to_thread(
        get_activity_direction,
        actor=actor,
        direction_id=direction_id,
    )
    bindings = await asyncio.to_thread(
        list_activity_direction_bindings,
        actor=actor,
        direction_id=direction.id,
    )
    counts = {"program": 0, "offering": 0, "event": 0}
    for binding in bindings:
        counts[binding.subject_kind.value] += 1
    await state.clear()
    await callback.answer()
    rows: list[list[tuple[str, str]]] = []
    if direction.status == ActivityDirectionStatus.ACTIVE:
        rows.append(
            [
                (
                    "📦 Убрать в архив",
                    f"cp:dirarc:{business_token}:{direction_token}",
                )
            ]
        )
    else:
        rows.append(
            [
                (
                    "♻️ Вернуть в работу",
                    f"cp:dirrestore:{business_token}:{direction_token}",
                )
            ]
        )
    rows.append([("⬅️ К направлениям", f"cp:dirs:{business_token}")])
    await control._callback_message(callback).answer(
        f"🧭 {direction.title}\n\n"
        f"{direction.description}\n\n"
        "Связано с направлением:\n"
        f"• материалов и программ: {counts['program']}\n"
        f"• услуг и предложений: {counts['offering']}\n"
        f"• событий: {counts['event']}\n\n"
        "Направление — это часть организации, а не отдельная организация.",
        reply_markup=control._keyboard(rows),
    )


@router.callback_query(F.data.startswith("cp:dirarc:"))
async def archive_direction(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, direction_token = str(callback.data).split(":", 3)
    business_id = control._token_uuid(business_token)
    direction_id = control._token_uuid(direction_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    direction = await asyncio.to_thread(
        archive_activity_direction,
        actor=actor,
        direction_id=direction_id,
    )
    await state.clear()
    await callback.answer("Направление архивировано")
    await control._callback_message(callback).answer(
        f"Направление «{direction.title}» убрано из активной работы. "
        "Связи и история сохранены.",
        reply_markup=control._keyboard(
            [[("⬅️ К направлениям", f"cp:dirs:{business_token}")]]
        ),
    )


@router.callback_query(F.data.startswith("cp:dirrestore:"))
async def restore_direction(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, direction_token = str(callback.data).split(":", 3)
    business_id = control._token_uuid(business_token)
    direction_id = control._token_uuid(direction_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    direction = await asyncio.to_thread(
        restore_activity_direction,
        actor=actor,
        direction_id=direction_id,
    )
    await state.clear()
    await callback.answer("Направление восстановлено")
    await control._callback_message(callback).answer(
        f"Направление «{direction.title}» снова активно.",
        reply_markup=control._keyboard(
            [[("⬅️ К направлениям", f"cp:dirs:{business_token}")]]
        ),
    )


__all__ = ["ClientPlatformActivityDirectionState", "router"]

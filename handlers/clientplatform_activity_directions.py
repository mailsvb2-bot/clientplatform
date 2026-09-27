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
    update_activity_direction,
)
from clientplatform.domain.activity_directions import (
    ActivityDirectionInvariantViolation,
    ActivityDirectionStatus,
)

from . import clientplatform_direction_context as direction_context

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_activity_directions")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())


class ClientPlatformActivityDirectionState(StatesGroup):
    title = State()
    description = State()
    edit_title = State()
    edit_description = State()


def _open_callback(business_id: str, direction_id: str) -> str:
    return (
        f"cp:diropen:{control._uuid_token(business_id)}:"
        f"{control._uuid_token(direction_id)}"
    )


def _routed_callback(callback: CallbackQuery, data: str) -> CallbackQuery:
    copier = getattr(callback, "model_copy", None)
    if callable(copier):
        return copier(update={"data": data})
    callback.data = data
    return callback


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
    except ActivityDirectionInvariantViolation:
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
        rows.extend(
            [
                [("📣 Продвижение и реклама", f"cp:dg:a:{business_token}:{direction_token}")],
                [("👥 Клиенты и продажи", f"cp:dg:c:{business_token}:{direction_token}")],
                [("🎥 Вебинары и мероприятия", f"cp:dg:e:{business_token}:{direction_token}")],
                [("📅 Запись и календарь", f"cp:dg:w:{business_token}:{direction_token}")],
                [("🎓 Материалы и программы", f"cp:dg:p:{business_token}:{direction_token}")],
                [("📊 Результаты", f"cp:dg:r:{business_token}:{direction_token}")],
                [("⚙️ Настроить направление", f"cp:dirset:{business_token}:{direction_token}")],
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
        "Что хотите сделать с этим направлением?\n\n"
        "Связано сейчас:\n"
        f"• материалов: {counts['program']}\n"
        f"• услуг и предложений: {counts['offering']}\n"
        f"• событий: {counts['event']}\n\n"
        "Направление — часть организации, а не отдельная организация: общий бренд, "
        "сотрудники, клиенты и подключения не дублируются.",
        reply_markup=control._keyboard(rows),
    )


@router.callback_query(F.data.startswith("cp:dirset:"))
async def open_activity_direction_settings(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    _, _, business_token, direction_token = str(callback.data).split(":", 3)
    business_id = control._token_uuid(business_token)
    direction_id = control._token_uuid(direction_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    direction = await asyncio.to_thread(
        get_activity_direction,
        actor=actor,
        direction_id=direction_id,
    )
    actor.assert_can_manage_business()
    await state.clear()
    await callback.answer()
    rows: list[list[tuple[str, str]]] = []
    if direction.status == ActivityDirectionStatus.ACTIVE:
        rows.extend(
            [
                [("✏️ Изменить", f"cp:diredit:{business_token}:{direction_token}")],
                [("🗑 Удалить направление", f"cp:dirarc:{business_token}:{direction_token}")],
                [("⬅️ К направлению", f"cp:diropen:{business_token}:{direction_token}")],
            ]
        )
    else:
        rows.extend(
            [
                [("♻️ Вернуть в работу", f"cp:dirrestore:{business_token}:{direction_token}")],
                [("📦 К архиву", f"cp:dirarch:{business_token}")],
            ]
        )
    await control._callback_message(callback).answer(
        f"⚙️ Настроить направление\n\n"
        f"🧭 {direction.title}\n"
        f"{direction.description}\n\n"
        "Здесь меняются только название, описание и статус направления. "
        "Рабочие действия находятся на экране самого направления.",
        reply_markup=control._keyboard(rows),
    )


@router.callback_query(F.data.startswith("cp:dg:"))
async def open_activity_direction_goal(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    _, _, goal, business_token, direction_token = str(callback.data).split(":", 4)
    business_id = control._token_uuid(business_token)
    direction_id = control._token_uuid(direction_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    direction = await asyncio.to_thread(
        get_activity_direction,
        actor=actor,
        direction_id=direction_id,
    )
    if direction.status != ActivityDirectionStatus.ACTIVE:
        await callback.answer("Направление больше не активно.", show_alert=True)
        return
    await direction_context.set_direction_context(
        state,
        business_id=business_id,
        direction_id=direction.id,
        title=direction.title,
    )

    if goal == "a":
        owner = importlib.import_module(".clientplatform_one_click_experience", __package__)
        await owner.open_ad_tools(
            _routed_callback(callback, f"cpo:ads:{business_token}:{direction_token}"),
            state,
        )
        return
    if goal == "c":
        owner = importlib.import_module(".clientplatform_one_click_experience", __package__)
        await owner.open_client_tools(
            _routed_callback(callback, f"cpo:clients:{business_token}:{direction_token}"),
            state,
        )
        return
    if goal == "e":
        events = importlib.import_module(".clientplatform_events", __package__)
        await events.open_event_hub(
            _routed_callback(callback, f"cpev:home:{business_token}:{direction_token}"),
            state,
        )
        return
    if goal == "w":
        owner = importlib.import_module(".clientplatform_one_click_experience", __package__)
        await owner.open_work_tools(
            _routed_callback(callback, f"cpo:work:{business_token}:{direction_token}"),
            state,
        )
        return
    if goal == "p":
        programs = importlib.import_module(".clientplatform_program_builder", __package__)
        await programs.open_programs(
            _routed_callback(callback, f"cp:cap:{business_token}:programs"),
            state,
        )
        return
    if goal == "r":
        await control.show_results(
            _routed_callback(callback, f"cp:results:{business_token}:{direction_token}"),
            state,
        )
        return
    await callback.answer("Кнопка устарела. Откройте направление заново.", show_alert=True)


@router.callback_query(F.data.startswith("cp:diredit:"))
async def begin_edit_direction(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, direction_token = str(callback.data).split(":", 3)
    business_id = control._token_uuid(business_token)
    direction_id = control._token_uuid(direction_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    direction = await asyncio.to_thread(
        get_activity_direction,
        actor=actor,
        direction_id=direction_id,
    )
    if direction.status != ActivityDirectionStatus.ACTIVE:
        await callback.answer(
            "Сначала верните направление в работу.",
            show_alert=True,
        )
        return
    await state.clear()
    await state.update_data(
        activity_direction_business_id=business_id,
        activity_direction_id=direction_id,
        activity_direction_original_description=direction.description,
    )
    await state.set_state(ClientPlatformActivityDirectionState.edit_title)
    await callback.answer()
    await control._callback_message(callback).answer(
        f"Текущее название: «{direction.title}».\n\n"
        "Напишите новое название направления."
    )


@router.message(ClientPlatformActivityDirectionState.edit_title)
async def capture_edit_direction_title(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("activity_direction_business_id") or "").strip()
    direction_id = str(data.get("activity_direction_id") or "").strip()
    title = " ".join(str(message.text or "").split()).strip()
    if not business_id or not direction_id:
        await state.clear()
        await message.answer(
            "Редактирование было закрыто. Откройте «Направления деятельности» заново."
        )
        return
    if not title or len(title) > 160:
        await message.answer("Название должно содержать от 1 до 160 символов.")
        return
    await state.update_data(activity_direction_edit_title=title)
    await state.set_state(ClientPlatformActivityDirectionState.edit_description)
    await message.answer(
        "Теперь напишите новое описание направления."
    )


@router.message(ClientPlatformActivityDirectionState.edit_description)
async def capture_edit_direction_description(
    message: Message,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    business_id = str(data.get("activity_direction_business_id") or "").strip()
    direction_id = str(data.get("activity_direction_id") or "").strip()
    title = str(data.get("activity_direction_edit_title") or "").strip()
    description = " ".join(str(message.text or "").split()).strip()
    if not business_id or not direction_id or not title:
        await state.clear()
        await message.answer(
            "Редактирование было закрыто. Откройте «Направления деятельности» заново."
        )
        return
    if not description or len(description) > 2000:
        await message.answer("Описание должно содержать от 1 до 2000 символов.")
        return
    actor = await control._actor(control._user_id(message), business_id)
    try:
        direction = await asyncio.to_thread(
            update_activity_direction,
            actor=actor,
            direction_id=direction_id,
            title=title,
            description=description,
        )
    except ActivityDirectionInvariantViolation:
        await message.answer(
            "Не удалось сохранить изменения. Проверьте название и попробуйте ещё раз."
        )
        return
    await state.clear()
    await message.answer(
        f"✅ Направление «{direction.title}» обновлено. "
        "Все существующие связи и история сохранены."
    )
    await _render_directions(
        message,
        user_id=control._user_id(message),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cp:dirarc:"))
async def archive_direction(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, direction_token = str(callback.data).split(":", 3)
    business_id = control._token_uuid(business_token)
    direction_id = control._token_uuid(direction_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    direction = await asyncio.to_thread(
        get_activity_direction,
        actor=actor,
        direction_id=direction_id,
    )
    if direction.status != ActivityDirectionStatus.ACTIVE:
        await callback.answer("Направление уже не активно.", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await control._callback_message(callback).answer(
        f"🗑 Удалить направление «{direction.title}»?\n\n"
        "Оно исчезнет из активной работы, но связанные материалы, услуги, "
        "вебинары и история сохранятся в архиве. Направление можно будет восстановить.",
        reply_markup=control._keyboard(
            [
                [
                    (
                        "🗑 Да, удалить направление",
                        f"cp:dirarcok:{business_token}:{direction_token}",
                    )
                ],
                [("Отмена", f"cp:diropen:{business_token}:{direction_token}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cp:dirarcok:"))
async def archive_direction_confirm(callback: CallbackQuery, state: FSMContext) -> None:
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
    await callback.answer("Направление удалено из активной работы")
    await control._callback_message(callback).answer(
        f"✅ Направление «{direction.title}» удалено из активной работы. "
        "Связи и история сохранены в архиве — направление можно восстановить.",
        reply_markup=control._keyboard(
            [
                [("📦 Архив направлений", f"cp:dirarch:{business_token}")],
                [("⬅️ К направлениям", f"cp:dirs:{business_token}")],
            ]
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

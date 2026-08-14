from __future__ import annotations

import asyncio
from aiogram import F, Router
from aiogram.filters import BaseFilter, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from clientplatform.application.activity import (
    claim_customer_invite,
    complete_business_profile,
    create_business_offering,
    enable_business_capability,
    get_business_profile,
    issue_customer_invite,
    list_business_capabilities,
    list_business_offerings,
    save_business_profile,
)
from clientplatform.application.booking_reminders import schedule_booking_reminders
from clientplatform.application.bookings import (
    book_customer_slot,
    create_booking_slot,
    list_booking_slots,
    list_customer_booking_slots,
    list_customer_businesses,
)
from clientplatform.application.customer_activity import (
    record_customer_contact,
    tenant_customer_activity,
)
from clientplatform.application.pagination import paginate
from clientplatform.application.control import (
    business_delivery_summary,
    create_single_lesson_program,
    prepare_program_delivery,
)
from clientplatform.application.control_callbacks import token_uuid as _token_uuid, uuid_token as _uuid_token
from clientplatform.application.customers import list_customers
from clientplatform.application.programs import list_programs
from clientplatform.application.progress import (
    complete_customer_lesson,
    get_customer_program,
    list_business_program_progress,
    list_customer_programs,
)
from clientplatform.application.tenancy import (
    create_business,
    list_accessible_businesses,
    resolve_tenant_context,
)
from clientplatform.domain.activity import (
    ACTIVITY_CONNECTORS,
    ActivityError,
    ActivityNotFound,
    CapabilityStatus,
)
from clientplatform.domain.booking_calendar import (
    booking_calendar_filename,
    booking_calendar_ics,
    google_calendar_url,
)
from clientplatform.domain.bookings import BookingError, BookingSlotStatus, BookingSlotView
from clientplatform.domain.programs import ContentKind, ProgramError
from clientplatform.domain.tenancy import TenancyError
from clientplatform.runtime.control_bot import control_bot_enabled
from config.settings import settings

router = Router(name="clientplatform_control")


class ClientPlatformControlEnabled(BaseFilter):
    async def __call__(self, _event: object) -> bool:
        return control_bot_enabled()


router.message.filter(ClientPlatformControlEnabled())
router.callback_query.filter(ClientPlatformControlEnabled())


class ClientPlatformControlState(StatesGroup):
    business_name = State()
    activity_description = State()
    custom_capability_title = State()
    offering_title = State()
    offering_description = State()
    program_title = State()
    lesson_title = State()
    lesson_content = State()
    booking_start = State()
    booking_duration = State()


def _keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def _user_id(message: Message) -> int:
    if message.from_user is None:
        raise ValueError("clientplatform requires a Telegram user")
    return int(message.from_user.id)


def _callback_message(callback: CallbackQuery) -> Message:
    if not isinstance(callback.message, Message):
        raise ValueError("clientplatform callback has no accessible message")
    return callback.message


def _start_payload(message: Message) -> str:
    parts = str(message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def _short_contact_time(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "—"
    return raw.replace("T", " ")[:16]


def _platform_label(value: str) -> str:
    return {"telegram": "Telegram", "vk": "VK", "max": "MAX"}.get(value, value.upper())


def _customer_activity_text(summary) -> str:
    channels = " · ".join(
        f"{_platform_label(platform)} {summary.by_platform.get(platform, 0)}"
        for platform in ("telegram", "vk", "max")
    )
    lines = [
        "Активность клиентов",
        "",
        f"Всего клиентов: {summary.total}",
        f"Новые сегодня: {summary.new_today}",
        f"Новые за 7 дней: {summary.new_7d}",
        f"Активны сегодня: {summary.active_today}",
        f"Каналы: {channels}",
        "",
        "Последние контакты:",
    ]
    if not summary.recent:
        lines.append("• Пока нет подключённых клиентов.")
        return "\n".join(lines)
    for row in summary.recent:
        name = row.display_name or "Клиент"
        handle = f" @{row.username}" if row.username else ""
        platforms = "/".join(_platform_label(item) for item in row.platforms) or "—"
        lines.append(
            f"• {name}{handle}\n"
            f"  {platforms} · первый {_short_contact_time(row.first_contact_at)} · "
            f"последний {_short_contact_time(row.last_contact_at)}"
        )
    return "\n".join(lines)


async def _touch_customer_callback(callback: CallbackQuery, *, business_id: str) -> None:
    user = callback.from_user
    await asyncio.to_thread(
        record_customer_contact,
        business_id=business_id,
        platform="telegram",
        external_subject=str(user.id),
        username=user.username,
        display_name=user.full_name,
    )


def _business_choice_keyboard(accesses: list[object]) -> InlineKeyboardMarkup:
    return _keyboard(
        [
            [
                (
                    access.business.name,
                    f"cp:business:{_uuid_token(access.business.id)}",
                )
            ]
            for access in accesses
        ]
    )


def _capability_setup_keyboard(business_id: str, active_keys: set[str]) -> InlineKeyboardMarkup:
    business_token = _uuid_token(business_id)
    rows: list[list[tuple[str, str]]] = []
    for connector in ACTIVITY_CONNECTORS.values():
        marker = "✅" if connector.key in active_keys else "➕"
        rows.append(
            [
                (
                    f"{marker} {connector.title}",
                    f"cp:toggle:{business_token}:{connector.key}",
                )
            ]
        )
    rows.append([("Готово", f"cp:finish:{business_token}")])
    return _keyboard(rows)


def _client_business_keyboard(links: list[object]) -> InlineKeyboardMarkup:
    return _keyboard(
        [
            [
                (
                    link.business_name,
                    f"cp:client:{_uuid_token(link.business_id)}",
                )
            ]
            for link in links
        ]
    )


def _client_portal_keyboard(business_id: str) -> InlineKeyboardMarkup:
    token = _uuid_token(business_id)
    return _keyboard(
        [
            [("Мои программы", f"cp:cprograms:{token}")],
            [("Посмотреть доступную запись", f"cp:client:{token}")],
        ]
    )


def _dashboard_keyboard(business_id: str, capabilities: list[object]) -> InlineKeyboardMarkup:
    token = _uuid_token(business_id)
    rows = [
        [(capability.title, f"cp:cap:{token}:{capability.connector_key}")]
        for capability in capabilities
        if capability.status == CapabilityStatus.ACTIVE
    ]
    rows.extend(
        [
            [("Активность клиентов", f"cp:clients:{token}"), ("Результаты", f"cp:results:{token}")],
            [("Изменить деятельность", f"cp:editact:{token}")],
        ]
    )
    return _keyboard(rows)


async def _actor(user_id: int, business_id: str):
    return await asyncio.to_thread(
        resolve_tenant_context,
        user_id=user_id,
        business_id=business_id,
    )


async def _send_capability_setup(message: Message, *, user_id: int, business_id: str) -> None:
    actor = await _actor(user_id, business_id)
    capabilities = await asyncio.to_thread(
        list_business_capabilities,
        actor=actor,
        include_disabled=True,
    )
    active = {
        capability.connector_key
        for capability in capabilities
        if capability.status == CapabilityStatus.ACTIVE
    }
    await message.answer(
        "Что Вы делаете для клиентов? Можно выбрать несколько вариантов. "
        "Новые форматы можно будет подключить позже.",
        reply_markup=_capability_setup_keyboard(business_id, active),
    )


async def _send_client_portal(message: Message, *, links: list[object]) -> None:
    if len(links) == 1:
        link = links[0]
        await message.answer(
            f"Вы подключены к «{link.business_name}».\n\n"
            "Здесь можно выбрать доступное время консультации или услуги.",
            reply_markup=_client_portal_keyboard(link.business_id),
        )
        return
    await message.answer(
        "Выберите специалиста или бизнес:",
        reply_markup=_client_business_keyboard(links),
    )


async def _send_dashboard(message: Message, *, user_id: int, business_id: str) -> None:
    actor = await _actor(user_id, business_id)
    profile = await asyncio.to_thread(get_business_profile, actor=actor)
    capabilities = await asyncio.to_thread(list_business_capabilities, actor=actor)
    access = next(
        access
        for access in await asyncio.to_thread(list_accessible_businesses, user_id=user_id)
        if access.business.id == business_id
    )
    module_lines = "\n".join(f"• {item.title}" for item in capabilities) or "• пока не выбраны"
    await message.answer(
        f"{access.business.name}\n\n"
        f"Чем Вы занимаетесь:\n{profile.activity_description}\n\n"
        f"Подключено:\n{module_lines}\n\n"
        "Выберите результат, который нужен сейчас.",
        reply_markup=_dashboard_keyboard(business_id, capabilities),
    )


async def _resume_business(message: Message, *, user_id: int, business_id: str, state: FSMContext) -> None:
    actor = await _actor(user_id, business_id)
    try:
        await asyncio.to_thread(get_business_profile, actor=actor)
    except ActivityNotFound:
        await state.set_state(ClientPlatformControlState.activity_description)
        await state.update_data(business_id=business_id, editing_activity=False)
        await message.answer(
            "Расскажите своими словами, чем Вы занимаетесь и чем помогаете клиентам.\n\n"
            "Например: «Консультирую родителей по вопросам сна детей» или "
            "«Ремонтирую автомобили и принимаю заказы на обслуживание»."
        )
        return
    await state.clear()
    await _send_dashboard(message, user_id=user_id, business_id=business_id)


@router.message(CommandStart())
async def clientplatform_start(message: Message, state: FSMContext) -> None:
    user_id = _user_id(message)
    payload = _start_payload(message)
    if payload.startswith("cpj_"):
        token = payload.removeprefix("cpj_")
        user = message.from_user
        claim = await asyncio.to_thread(
            claim_customer_invite,
            token=token,
            telegram_user_id=user_id,
            username=None if user is None else user.username,
            display_name=None if user is None else user.full_name,
        )
        await asyncio.to_thread(
            record_customer_contact,
            business_id=claim.business_id,
            platform="telegram",
            external_subject=str(user_id),
            username=None if user is None else user.username,
            display_name=None if user is None else user.full_name,
        )
        await state.clear()
        detail = "Вы уже были подключены." if claim.already_connected else "Подключение завершено."
        await message.answer(
            f"Вы подключены к «{claim.business_name}». {detail}\n"
            "Материалы и сообщения этого специалиста будут приходить сюда.",
            reply_markup=_client_portal_keyboard(claim.business_id),
        )
        return

    accesses = await asyncio.to_thread(list_accessible_businesses, user_id=user_id)
    if not accesses:
        links = await asyncio.to_thread(list_customer_businesses, telegram_user_id=user_id)
        if links:
            await state.clear()
            await _send_client_portal(message, links=links)
            return
        await state.set_state(ClientPlatformControlState.business_name)
        await message.answer(
            "Добро пожаловать в ClientPlatform.\n\n"
            "Сначала напишите название Вашего дела, проекта или практики."
        )
        return
    if len(accesses) > 1:
        await state.clear()
        await message.answer(
            "Выберите бизнес, с которым хотите работать:",
            reply_markup=_business_choice_keyboard(accesses),
        )
        return
    await _resume_business(
        message,
        user_id=user_id,
        business_id=accesses[0].business.id,
        state=state,
    )


@router.message(ClientPlatformControlState.business_name)
async def receive_business_name(message: Message, state: FSMContext) -> None:
    access = await asyncio.to_thread(
        create_business,
        owner_user_id=_user_id(message),
        name=str(message.text or ""),
    )
    await state.set_state(ClientPlatformControlState.activity_description)
    await state.update_data(business_id=access.business.id, editing_activity=False)
    await message.answer(
        "Теперь напишите своими словами, чем Вы занимаетесь и что делаете для клиентов. "
        "Можно описать любую деятельность — готового списка профессий нет."
    )


@router.message(ClientPlatformControlState.activity_description)
async def receive_activity_description(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data["business_id"])
    actor = await _actor(_user_id(message), business_id)
    await asyncio.to_thread(
        save_business_profile,
        actor=actor,
        activity_description=str(message.text or ""),
        timezone_name=settings.TIMEZONE,
    )
    if bool(data.get("editing_activity")):
        await state.clear()
        await message.answer("Описание деятельности обновлено.")
        await _send_dashboard(message, user_id=_user_id(message), business_id=business_id)
        return

    for connector_key in ("programs", "consultations", "services"):
        await asyncio.to_thread(
            enable_business_capability,
            actor=actor,
            connector_key=connector_key,
        )
    await asyncio.to_thread(complete_business_profile, actor=actor)
    await state.clear()
    await message.answer(
        "✅ Всё готово.\n\n"
        "Я создал рабочее пространство, где можно подключать клиентов, "
        "назначать встречи, выдавать материалы и видеть результат. "
        "Технические настройки Вам не понадобятся."
    )
    await _send_dashboard(message, user_id=_user_id(message), business_id=business_id)


@router.callback_query(F.data.startswith("cp:business:"))
async def choose_business(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = _token_uuid(str(callback.data).split(":", 2)[2])
    await callback.answer()
    await _resume_business(
        _callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        state=state,
    )


@router.callback_query(F.data.startswith("cp:toggle:"))
async def toggle_capability(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, connector_key = str(callback.data).split(":", 3)
    business_id = _token_uuid(business_token)
    actor = await _actor(int(callback.from_user.id), business_id)
    capabilities = await asyncio.to_thread(
        list_business_capabilities,
        actor=actor,
        include_disabled=True,
    )
    existing = next((item for item in capabilities if item.connector_key == connector_key), None)
    if existing is not None and existing.status == CapabilityStatus.ACTIVE:
        from clientplatform.application.activity import disable_business_capability

        await asyncio.to_thread(
            disable_business_capability,
            actor=actor,
            connector_key=connector_key,
        )
    elif connector_key == "custom":
        await state.set_state(ClientPlatformControlState.custom_capability_title)
        await state.update_data(business_id=business_id)
        await callback.answer()
        await _callback_message(callback).answer(
            "Как называется Ваш дополнительный формат работы? Напишите своими словами."
        )
        return
    else:
        await asyncio.to_thread(
            enable_business_capability,
            actor=actor,
            connector_key=connector_key,
        )
    await callback.answer("Обновлено")
    await _send_capability_setup(
        _callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.message(ClientPlatformControlState.custom_capability_title)
async def receive_custom_capability_title(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data["business_id"])
    actor = await _actor(_user_id(message), business_id)
    await asyncio.to_thread(
        enable_business_capability,
        actor=actor,
        connector_key="custom",
        title=str(message.text or ""),
    )
    await state.clear()
    await _send_capability_setup(message, user_id=_user_id(message), business_id=business_id)


@router.callback_query(F.data.startswith("cp:finish:"))
async def finish_profile(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = _token_uuid(str(callback.data).split(":", 2)[2])
    actor = await _actor(int(callback.from_user.id), business_id)
    try:
        await asyncio.to_thread(complete_business_profile, actor=actor)
    except ActivityError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Готово")
    await state.clear()
    await _send_dashboard(
        _callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cp:editact:"))
async def edit_activity(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = _token_uuid(str(callback.data).split(":", 2)[2])
    await state.set_state(ClientPlatformControlState.activity_description)
    await state.update_data(business_id=business_id, editing_activity=True)
    await callback.answer()
    await _callback_message(callback).answer("Напишите новое описание Вашей деятельности.")


@router.callback_query(F.data.startswith("cp:cap:"))
async def open_capability(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, connector_key = str(callback.data).split(":", 3)
    business_id = _token_uuid(business_token)
    actor = await _actor(int(callback.from_user.id), business_id)
    capabilities = await asyncio.to_thread(list_business_capabilities, actor=actor)
    capability = next(item for item in capabilities if item.connector_key == connector_key)
    await state.update_data(business_id=business_id, capability_id=capability.id)
    await callback.answer()
    message = _callback_message(callback)
    if connector_key == "programs":
        programs = await asyncio.to_thread(list_programs, actor=actor)
        lines = "\n".join(f"• {program.title}" for program in programs) or "Пока нет программ."
        await message.answer(
            f"{capability.title}\n\n{lines}",
            reply_markup=_keyboard(
                [
                    [("Создать программу", f"cp:progadd:{business_token}")],
                    [("Выдать клиенту", f"cp:deliver:{business_token}")],
                ]
            ),
        )
        return
    offerings = await asyncio.to_thread(
        list_business_offerings,
        actor=actor,
        capability_id=capability.id,
    )
    slots = await asyncio.to_thread(list_booking_slots, actor=actor)
    open_counts: dict[str, int] = {}
    for slot in slots:
        if slot.slot.status == BookingSlotStatus.OPEN:
            open_counts[slot.slot.offering_id] = open_counts.get(slot.slot.offering_id, 0) + 1
    lines = "\n".join(
        f"• {offering.title} — {offering.description} "
        f"(свободных времён: {open_counts.get(offering.id, 0)})"
        for offering in offerings
    ) or "Пока нет добавленных предложений."
    rows = [
        [
            (
                f"Добавить время · {offering.title[:24]}",
                f"cp:slotadd:{business_token}:{_uuid_token(offering.id)}",
            )
        ]
        for offering in offerings
    ]
    rows.append(
        [("Добавить предложение", f"cp:offeradd:{business_token}:{_uuid_token(capability.id)}")]
    )
    await message.answer(
        f"{capability.title}\n\n{lines}",
        reply_markup=_keyboard(rows),
    )


@router.callback_query(F.data.startswith("cp:offeradd:"))
async def start_offering(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, capability_token = str(callback.data).split(":", 3)
    await state.set_state(ClientPlatformControlState.offering_title)
    await state.update_data(
        business_id=_token_uuid(business_token),
        capability_id=_token_uuid(capability_token),
    )
    await callback.answer()
    await _callback_message(callback).answer("Как называется консультация, услуга или предложение?")


@router.message(ClientPlatformControlState.offering_title)
async def receive_offering_title(message: Message, state: FSMContext) -> None:
    await state.update_data(offering_title=str(message.text or ""))
    await state.set_state(ClientPlatformControlState.offering_description)
    await message.answer("Коротко опишите, что получает клиент.")


@router.message(ClientPlatformControlState.offering_description)
async def receive_offering_description(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data["business_id"])
    actor = await _actor(_user_id(message), business_id)
    offering = await asyncio.to_thread(
        create_business_offering,
        actor=actor,
        capability_id=str(data["capability_id"]),
        title=str(data["offering_title"]),
        description=str(message.text or ""),
    )
    await state.clear()
    await message.answer(f"Добавлено: {offering.title}")
    await _send_dashboard(message, user_id=_user_id(message), business_id=business_id)


@router.callback_query(F.data.startswith("cp:slotadd:"))
async def start_booking_slot(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, offering_token = str(callback.data).split(":", 3)
    await state.set_state(ClientPlatformControlState.booking_start)
    await state.update_data(
        business_id=_token_uuid(business_token),
        offering_id=_token_uuid(offering_token),
    )
    await callback.answer()
    await _callback_message(callback).answer(
        "Напишите дату и время по местному времени бизнеса в формате "
        "ДД.ММ.ГГГГ ЧЧ:ММ. Например: 31.07.2026 15:00"
    )


@router.message(ClientPlatformControlState.booking_start)
async def receive_booking_start(message: Message, state: FSMContext) -> None:
    await state.update_data(booking_start=str(message.text or ""))
    await state.set_state(ClientPlatformControlState.booking_duration)
    await message.answer("Сколько минут длится встреча или услуга? Например: 60")


@router.message(ClientPlatformControlState.booking_duration)
async def receive_booking_duration(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data["business_id"])
    actor = await _actor(_user_id(message), business_id)
    slot = await asyncio.to_thread(
        create_booking_slot,
        actor=actor,
        offering_id=str(data["offering_id"]),
        local_start=str(data["booking_start"]),
        duration_minutes=int(str(message.text or "").strip()),
    )
    await state.clear()
    await message.answer(
        f"Время опубликовано: {slot.offering_title} — {slot.local_start}, "
        f"{slot.slot.duration_minutes} мин."
    )
    await _send_dashboard(message, user_id=_user_id(message), business_id=business_id)


@router.callback_query(F.data.startswith("cp:cprograms:"))
async def open_customer_programs(callback: CallbackQuery) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = _token_uuid(business_token)
    programs = await asyncio.to_thread(
        list_customer_programs,
        telegram_user_id=int(callback.from_user.id),
        business_id=business_id,
    )
    await _touch_customer_callback(callback, business_id=business_id)
    await callback.answer()
    message = _callback_message(callback)
    if not programs:
        await message.answer("Вам пока не выдали ни одной программы.")
        return
    lines = "\n".join(
        f"• {item.program_title} — {item.completed_lessons}/{item.total_lessons} "
        f"({item.percent_complete}%)"
        for item in programs
    )
    await message.answer(
        f"Мои программы\n\n{lines}\n\nВыберите программу:",
        reply_markup=_keyboard(
            [
                [
                    (
                        item.program_title[:36],
                        f"cp:cprog:{business_token}:{_uuid_token(item.enrollment_id)}",
                    )
                ]
                for item in programs
            ]
        ),
    )


@router.callback_query(F.data.startswith("cp:cprog:"))
async def open_customer_program(callback: CallbackQuery) -> None:
    _, _, business_token, enrollment_token = str(callback.data).split(":", 3)
    business_id = _token_uuid(business_token)
    enrollment_id = _token_uuid(enrollment_token)
    program = await asyncio.to_thread(
        get_customer_program,
        telegram_user_id=int(callback.from_user.id),
        business_id=business_id,
        enrollment_id=enrollment_id,
    )
    await _touch_customer_callback(callback, business_id=business_id)
    icons = {
        "pending": "⏳",
        "delivered": "📬",
        "opened": "👀",
        "completed": "✅",
        "skipped": "⏭",
    }
    lines = "\n".join(
        f"{icons.get(lesson.progress_status.value, '•')} {lesson.position}. {lesson.title}"
        for lesson in program.lessons
    ) or "В программе пока нет материалов."
    rows = [
        [
            (
                f"Готово · урок {lesson.position}",
                f"cp:done:{business_token}:{enrollment_token}:{lesson.position}",
            )
        ]
        for lesson in program.lessons
        if lesson.can_complete
    ]
    rows.append([("Назад к программам", f"cp:cprograms:{business_token}")])
    await callback.answer()
    await _callback_message(callback).answer(
        f"{program.summary.program_title}\n\n{lines}\n\n"
        f"Пройдено: {program.summary.completed_lessons}/{program.summary.total_lessons} "
        f"({program.summary.percent_complete}%)",
        reply_markup=_keyboard(rows),
    )


@router.callback_query(F.data.startswith("cp:done:"))
async def complete_customer_program_lesson(callback: CallbackQuery) -> None:
    _, _, business_token, enrollment_token, position = str(callback.data).split(":", 4)
    business_id = _token_uuid(business_token)
    result = await asyncio.to_thread(
        complete_customer_lesson,
        telegram_user_id=int(callback.from_user.id),
        business_id=business_id,
        enrollment_id=_token_uuid(enrollment_token),
        lesson_position=int(position),
    )
    await _touch_customer_callback(callback, business_id=business_id)
    await callback.answer("Прогресс сохранён")
    if result.next_material_queued:
        detail = "Следующий материал уже поставлен в отправку."
    elif result.program.summary.enrollment_status.value == "completed":
        detail = "Программа завершена. Отличная работа!"
    else:
        detail = "Урок отмечен выполненным."
    await _callback_message(callback).answer(
        f"{detail}\n\n"
        f"Пройдено: {result.program.summary.completed_lessons}/"
        f"{result.program.summary.total_lessons} "
        f"({result.program.summary.percent_complete}%)",
        reply_markup=_keyboard(
            [[("Открыть программу", f"cp:cprog:{business_token}:{enrollment_token}")]]
        ),
    )


async def _send_client_booking_page(
    message: Message,
    *,
    business_token: str,
    slots: list[BookingSlotView],
    page: object = 0,
    title: str = "Доступная запись",
    empty_text: str = "Сейчас свободного времени нет. Специалист сможет добавить его позже.",
) -> None:
    if not slots:
        await message.answer(empty_text)
        return
    current = paginate(slots, page)
    lines = "\n".join(
        f"• {slot.offering_title} — {slot.local_start}, {slot.slot.duration_minutes} мин."
        for slot in current.items
    )
    rows = [
        [
            (
                f"{slot.local_start} · {slot.offering_title[:20]}",
                f"cp:book:{business_token}:{_uuid_token(slot.slot.id)}",
            )
        ]
        for slot in current.items
    ]
    navigation: list[tuple[str, str]] = []
    if current.has_previous:
        navigation.append(("⬅️ Назад", f"cp:client:{business_token}:{current.index - 1}"))
    if current.has_next:
        navigation.append(("Вперёд ➡️", f"cp:client:{business_token}:{current.index + 1}"))
    if navigation:
        rows.append(navigation)
    await message.answer(
        f"{title}\n\n{lines}\n\nСтраница {current.index + 1}/{current.count}\n\n"
        "Выберите удобное время:",
        reply_markup=_keyboard(rows),
    )


@router.callback_query(F.data.startswith("cp:client:"))
async def open_client_booking(callback: CallbackQuery) -> None:
    parts = str(callback.data or "").split(":")
    if len(parts) not in {3, 4}:
        await callback.answer("Кнопка устарела. Откройте запись заново.", show_alert=True)
        return
    _, _, business_token, *raw_page = parts
    business_id = _token_uuid(business_token)
    slots = await asyncio.to_thread(
        list_customer_booking_slots,
        telegram_user_id=int(callback.from_user.id),
        business_id=business_id,
    )
    await _touch_customer_callback(callback, business_id=business_id)
    await callback.answer()
    await _send_client_booking_page(
        _callback_message(callback),
        business_token=business_token,
        slots=slots,
        page=raw_page[0] if raw_page else 0,
    )


@router.callback_query(F.data.startswith("cp:book:"))
async def book_client_slot(callback: CallbackQuery) -> None:
    _, _, business_token, slot_token = str(callback.data).split(":", 3)
    business_id = _token_uuid(business_token)
    claim = await asyncio.to_thread(
        book_customer_slot,
        telegram_user_id=int(callback.from_user.id),
        business_id=business_id,
        slot_id=_token_uuid(slot_token),
    )
    await _touch_customer_callback(callback, business_id=business_id)
    await callback.answer("Запись подтверждена")
    message = _callback_message(callback)
    await message.answer(
        f"✅ Вы записаны: {claim.slot.offering_title} — {claim.slot.local_start}, "
        f"{claim.slot.slot.duration_minutes} мин.\n"
        f"Бизнес: {claim.slot.business_name}.\n\n"
        "Я также пришлю напоминания в Telegram. Ниже можно одним нажатием "
        "добавить встречу в календарь телефона."
    )

    slot = claim.slot.slot
    if all(hasattr(slot, name) for name in ("starts_at", "ends_at", "business_id", "id")):
        await asyncio.to_thread(
            schedule_booking_reminders,
            telegram_user_id=int(callback.from_user.id),
            claim=claim,
        )
        document_sender = getattr(message, "answer_document", None)
        if callable(document_sender):
            calendar = BufferedInputFile(
                booking_calendar_ics(claim.slot),
                filename=booking_calendar_filename(claim.slot),
            )
            await document_sender(
                calendar,
                caption=(
                    "📅 Нажмите на файл — телефон предложит добавить встречу "
                    "с напоминаниями за 24 часа и за 1 час."
                ),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text="Добавить в Google Календарь",
                            url=google_calendar_url(claim.slot),
                        )
                    ]]
                ),
            )


@router.callback_query(F.data.startswith("cp:progadd:"))
async def start_program(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = _token_uuid(str(callback.data).split(":", 2)[2])
    await state.set_state(ClientPlatformControlState.program_title)
    await state.update_data(business_id=business_id)
    await callback.answer()
    await _callback_message(callback).answer("Напишите название программы.")


@router.message(ClientPlatformControlState.program_title)
async def receive_program_title(message: Message, state: FSMContext) -> None:
    await state.update_data(program_title=str(message.text or ""))
    await state.set_state(ClientPlatformControlState.lesson_title)
    await message.answer("Как называется первый материал или урок?")


@router.message(ClientPlatformControlState.lesson_title)
async def receive_lesson_title(message: Message, state: FSMContext) -> None:
    await state.update_data(lesson_title=str(message.text or ""))
    await state.set_state(ClientPlatformControlState.lesson_content)
    await message.answer(
        "Теперь отправьте сам материал: аудио, голосовое сообщение, видео, документ, "
        "изображение или обычный текст."
    )


def _message_content(message: Message) -> tuple[ContentKind, str]:
    if message.audio is not None:
        return ContentKind.AUDIO, message.audio.file_id
    if message.voice is not None:
        return ContentKind.AUDIO, message.voice.file_id
    if message.video is not None:
        return ContentKind.VIDEO, message.video.file_id
    if message.document is not None:
        return ContentKind.DOCUMENT, message.document.file_id
    if message.photo:
        return ContentKind.IMAGE, message.photo[-1].file_id
    if str(message.text or "").strip():
        return ContentKind.TEXT, str(message.text).strip()
    raise ValueError("поддерживаются аудио, видео, документ, изображение или текст")


@router.message(ClientPlatformControlState.lesson_content)
async def receive_lesson_content(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    kind, content_ref = _message_content(message)
    business_id = str(data["business_id"])
    actor = await _actor(_user_id(message), business_id)
    program = await asyncio.to_thread(
        create_single_lesson_program,
        actor=actor,
        program_title=str(data["program_title"]),
        lesson_title=str(data["lesson_title"]),
        content_kind=kind,
        content_ref=content_ref,
    )
    await state.clear()
    await message.answer(
        f"Программа «{program.program.title}» создана и готова к выдаче клиентам."
    )
    await _send_dashboard(message, user_id=_user_id(message), business_id=business_id)


@router.callback_query(F.data.startswith("cp:clients:"))
async def open_clients(callback: CallbackQuery) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = _token_uuid(business_token)
    actor = await _actor(int(callback.from_user.id), business_id)
    summary = await asyncio.to_thread(tenant_customer_activity, actor=actor, limit=15)
    await callback.answer()
    await _callback_message(callback).answer(
        _customer_activity_text(summary),
        reply_markup=_keyboard([[('Подключить клиента', f"cp:invite:{business_token}")]]),
    )


@router.callback_query(F.data.startswith("cp:invite:"))
async def create_invite(callback: CallbackQuery) -> None:
    business_id = _token_uuid(str(callback.data).split(":", 2)[2])
    actor = await _actor(int(callback.from_user.id), business_id)
    issued = await asyncio.to_thread(issue_customer_invite, actor=actor)
    bot = await callback.bot.get_me()
    if not bot.username:
        raise RuntimeError("clientplatform control bot requires a public username for invites")
    link = f"https://t.me/{bot.username}?start=cpj_{issued.token}"
    await callback.answer("Ссылка создана")
    await _callback_message(callback).answer(
        "Отправьте эту ссылку клиенту. Она действует 7 дней и может быть использована один раз:\n\n"
        f"{link}"
    )


@router.callback_query(F.data.startswith("cp:deliver:"))
async def choose_program_for_delivery(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = _token_uuid(business_token)
    actor = await _actor(int(callback.from_user.id), business_id)
    programs = await asyncio.to_thread(list_programs, actor=actor)
    await state.update_data(business_id=business_id)
    await callback.answer()
    if not programs:
        await _callback_message(callback).answer("Сначала создайте хотя бы одну программу.")
        return
    await _callback_message(callback).answer(
        "Какую программу выдать?",
        reply_markup=_keyboard(
            [
                [(program.title, f"cp:sendp:{business_token}:{_uuid_token(program.id)}")]
                for program in programs
            ]
        ),
    )


@router.callback_query(F.data.startswith("cp:sendp:"))
async def choose_customer_for_delivery(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, program_token = str(callback.data).split(":", 3)
    business_id = _token_uuid(business_token)
    program_id = _token_uuid(program_token)
    actor = await _actor(int(callback.from_user.id), business_id)
    customers = await asyncio.to_thread(list_customers, actor=actor)
    await state.update_data(business_id=business_id, selected_program_id=program_id)
    await callback.answer()
    if not customers:
        await _callback_message(callback).answer("Сначала подключите клиента по персональной ссылке.")
        return
    await _callback_message(callback).answer(
        "Кому выдать программу?",
        reply_markup=_keyboard(
            [
                [
                    (
                        customer.display_name or "Клиент",
                        f"cp:sendc:{business_token}:{_uuid_token(customer.id)}",
                    )
                ]
                for customer in customers
            ]
        ),
    )


@router.callback_query(F.data.startswith("cp:sendc:"))
async def send_program_to_customer(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, customer_token = str(callback.data).split(":", 3)
    business_id = _token_uuid(business_token)
    customer_id = _token_uuid(customer_token)
    data = await state.get_data()
    program_id = str(data.get("selected_program_id") or "")
    if not program_id:
        await callback.answer("Выберите программу заново", show_alert=True)
        return
    actor = await _actor(int(callback.from_user.id), business_id)
    prepared = await asyncio.to_thread(
        prepare_program_delivery,
        actor=actor,
        program_id=program_id,
        customer_id=customer_id,
        bot_id=int(callback.bot.id),
    )
    await callback.answer("Поставлено в отправку")
    await _callback_message(callback).answer(
        f"Программа «{prepared.program.program.title}» поставлена в очередь. "
        "ClientPlatform отправит первый материал и сохранит результат доставки."
    )


@router.callback_query(F.data.startswith("cp:results:"))
async def show_results(callback: CallbackQuery) -> None:
    business_id = _token_uuid(str(callback.data).split(":", 2)[2])
    actor = await _actor(int(callback.from_user.id), business_id)
    summary = await asyncio.to_thread(business_delivery_summary, actor=actor)
    progress = await asyncio.to_thread(list_business_program_progress, actor=actor, limit=15)
    progress_lines = "\n".join(
        f"• {item.customer_display_name or 'Клиент'}: {item.program_title} — "
        f"{item.completed_lessons}/{item.total_lessons} ({item.percent_complete}%)"
        for item in progress
    ) or "Пока нет выданных программ."
    await callback.answer()
    await _callback_message(callback).answer(
        "Результаты\n\n"
        f"Клиенты: {summary.customers}\n"
        f"Активные программы: {summary.programs}\n"
        f"Ожидают отправки: {summary.dispatch_pending}\n"
        f"Успешно отправлено: {summary.dispatch_sent}\n"
        f"Требуют внимания: {summary.dispatch_attention}\n\n"
        f"Прогресс клиентов\n{progress_lines}"
    )


@router.errors()
async def clientplatform_control_error(event: object) -> bool:
    exception = getattr(event, "exception", None)
    update = getattr(event, "update", None)
    if not isinstance(
        exception,
        (ValueError, ActivityError, BookingError, TenancyError, ProgramError),
    ):
        return False
    message = getattr(update, "message", None)
    callback = getattr(update, "callback_query", None)
    if isinstance(message, Message):
        await message.answer(f"Не получилось выполнить действие: {exception}")
        return True
    if isinstance(callback, CallbackQuery):
        await callback.answer(f"Не получилось: {exception}", show_alert=True)
        return True
    return False

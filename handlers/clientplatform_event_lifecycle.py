from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.event_content_plans import set_event_content_mode
from clientplatform.application.cockpit_events import resolve_event_live_snapshot
from clientplatform.application.event_owner_flow import (
    MultiSessionOnlineEventCreateRequest,
    OnlineEventCreateRequest,
    OnlineEventSessionCreateRequest,
    create_and_publish_multisession_online_event,
    create_and_publish_online_event,
)
from clientplatform.application.event_sessions import (
    EventSessionSpec,
    configure_event_sessions,
    get_event_warmup_window,
    list_event_sessions,
)
from clientplatform.application.event_warmups import save_event_warmup_plan
from clientplatform.application.event_wizard import (
    MAX_EVENT_SESSIONS,
    EventWizardSession,
    normalize_event_timezone,
    normalize_session_join_url,
    parse_session_count,
    parse_session_window,
    validate_session_sequence,
)
from clientplatform.domain.event_content import (
    EventContentMode,
    EventContentStage,
    event_content_mode_label,
    parse_event_content_mode,
)
from clientplatform.domain.tenancy import TenantPermissionDenied
from clientplatform.presentation.event_schedule_picker import (
    MAX_CALENDAR_MONTHS,
    QUICK_DURATIONS,
    QUICK_START_TIMES,
    WEBINAR_VENUES,
    calendar_days,
    local_today,
    month_distance,
    month_key,
    parse_calendar_date,
    parse_month_key,
    parse_quick_duration,
    parse_quick_time,
    session_window_text,
    shift_month,
    webinar_venue,
)
from clientplatform.presentation.event_ui import BACK_TO_EVENTS_LABEL
from config.settings import settings

from . import clientplatform_control as control


router = Router(name="clientplatform_event_lifecycle")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())


class ClientPlatformEventLifecycleState(StatesGroup):
    waiting_title = State()
    waiting_days = State()
    waiting_topics_choice = State()
    waiting_topics = State()
    waiting_timezone = State()
    waiting_session_time = State()
    waiting_session_url = State()
    waiting_warmup_days = State()
    waiting_warmup_mode = State()
    waiting_event_day_mode = State()
    waiting_post_event_mode = State()


_RU_MONTHS = (
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)


def _cancel_keyboard(business_id: str):
    token = control._uuid_token(business_id)
    return control._keyboard([[(BACK_TO_EVENTS_LABEL, f"cpev:cancel:{token}")]])


def _timezone_keyboard(business_id: str):
    token = control._uuid_token(business_id)
    return control._keyboard(
        [
            [("🕒 Москва", "cpev:tz:moscow"), ("🌍 Другое время", "cpev:tz:other")],
            [(BACK_TO_EVENTS_LABEL, f"cpev:cancel:{token}")],
        ]
    )


def _topics_keyboard(business_id: str):
    token = control._uuid_token(business_id)
    return control._keyboard(
        [
            [
                ("Да, у дней есть темы", "cpev:topics:yes"),
                ("Нет, тема общая", "cpev:topics:no"),
            ],
            [(BACK_TO_EVENTS_LABEL, f"cpev:cancel:{token}")],
        ]
    )


def _venue_keyboard(business_id: str):
    token = control._uuid_token(business_id)
    rows: list[list[tuple[str, str]]] = []
    pair: list[tuple[str, str]] = []
    for venue in WEBINAR_VENUES:
        pair.append((venue.label, f"cpev:venue:{venue.key}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([(BACK_TO_EVENTS_LABEL, f"cpev:cancel:{token}")])
    return control._keyboard(rows)


def _calendar_keyboard(
    *,
    business_id: str,
    timezone_name: str,
    year: int,
    month: int,
    minimum_date,
) -> InlineKeyboardMarkup:
    token = control._uuid_token(business_id)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=f"{_RU_MONTHS[month]} {year}", callback_data="cpev:noop")],
        [
            InlineKeyboardButton(text=label, callback_data="cpev:noop")
            for label in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
        ],
    ]
    for week in calendar_days(year=year, month=month, minimum=minimum_date):
        rows.append(
            [
                InlineKeyboardButton(
                    text=str(cell.day) if cell.day else "·",
                    callback_data=f"cpev:date:{cell.value}" if cell.enabled else "cpev:noop",
                )
                for cell in week
            ]
        )
    distance = month_distance(minimum_date, year=year, month=month)
    navigation: list[InlineKeyboardButton] = []
    if distance > 0:
        previous_year, previous_month = shift_month(year, month, -1)
        navigation.append(
            InlineKeyboardButton(
                text=f"⬅️ {_RU_MONTHS[previous_month]}",
                callback_data=f"cpev:month:{month_key(previous_year, previous_month)}",
            )
        )
    if distance < MAX_CALENDAR_MONTHS:
        next_year, next_month = shift_month(year, month, 1)
        navigation.append(
            InlineKeyboardButton(
                text=f"{_RU_MONTHS[next_month]} ➡️",
                callback_data=f"cpev:month:{month_key(next_year, next_month)}",
            )
        )
    if navigation:
        rows.append(navigation)
    rows.append(
        [InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="cpev:manual-time")]
    )
    rows.append(
        [InlineKeyboardButton(text=BACK_TO_EVENTS_LABEL, callback_data=f"cpev:cancel:{token}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _start_time_keyboard(business_id: str) -> InlineKeyboardMarkup:
    token = control._uuid_token(business_id)
    rows = [
        [
            InlineKeyboardButton(text=value, callback_data=f"cpev:start:{value.replace(':', '')}")
            for value in QUICK_START_TIMES[index : index + 3]
        ]
        for index in range(0, len(QUICK_START_TIMES), 3)
    ]
    rows.append(
        [InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="cpev:manual-time")]
    )
    rows.append(
        [InlineKeyboardButton(text=BACK_TO_EVENTS_LABEL, callback_data=f"cpev:cancel:{token}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _duration_keyboard(business_id: str) -> InlineKeyboardMarkup:
    token = control._uuid_token(business_id)
    labels = {30: "30 мин", 60: "1 час", 90: "1,5 часа", 120: "2 часа", 180: "3 часа"}
    rows = [
        [
            InlineKeyboardButton(text=labels[value], callback_data=f"cpev:duration:{value}")
            for value in QUICK_DURATIONS[index : index + 3]
        ]
        for index in range(0, len(QUICK_DURATIONS), 3)
    ]
    rows.append(
        [InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="cpev:manual-time")]
    )
    rows.append(
        [InlineKeyboardButton(text=BACK_TO_EVENTS_LABEL, callback_data=f"cpev:cancel:{token}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _session_url_keyboard(*, business_id: str, venue_key: str) -> InlineKeyboardMarkup:
    venue = webinar_venue(venue_key)
    token = control._uuid_token(business_id)
    rows: list[list[InlineKeyboardButton]] = []
    if venue.open_url:
        rows.append(
            [InlineKeyboardButton(text=f"↗️ Открыть {venue.label}", url=venue.open_url)]
        )
    rows.append(
        [InlineKeyboardButton(text=BACK_TO_EVENTS_LABEL, callback_data=f"cpev:cancel:{token}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _public_base_url() -> str:
    value = str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if not value.startswith("https://"):
        raise ValueError("публичный HTTPS-адрес ClientPlatform пока не настроен")
    return value


def _normalized_text(message: Message) -> str:
    return " ".join(str(message.text or "").strip().split())


def _is_cancel(message: Message) -> bool:
    return _normalized_text(message).casefold() in {"отмена", "cancel"}


async def _cancel(message: Message, state: FSMContext, business_id: str) -> None:
    await state.clear()
    await message.answer(
        "Создание вебинара отменено. Данные не изменены.",
        reply_markup=control._keyboard(
            [[(BACK_TO_EVENTS_LABEL, f"cpev:home:{control._uuid_token(business_id)}")]]
        ),
    )


def _event_actions(
    *,
    event_id: str,
    business_id: str,
    join_ready: bool,
    visual_requested: bool = False,
) -> list[list[tuple[str, str]]]:
    business_token = control._uuid_token(business_id)
    event_token = control._uuid_token(event_id)
    rows: list[list[tuple[str, str]]] = []
    if not join_ready:
        rows.append([("🔗 Добавить ссылку на эфир", f"cpev:join:{event_token}:{business_token}")])
    rows.append([("✨ Сделать анонс", f"cpev:announce:{event_token}:{business_token}")])
    if visual_requested:
        rows.append([("🎨 Картинки и креативы", f"cpc:open:{business_token}")])
    rows.append([(BACK_TO_EVENTS_LABEL, f"cpev:home:{business_token}")])
    rows.append([("🎥 Создать ещё", f"cpev:new:{business_token}")])
    return rows


def _session_from_payload(value: object) -> EventWizardSession:
    if not isinstance(value, dict):
        raise ValueError("invalid session payload")
    raw_join_url = str(value.get("join_url") or "").strip()
    return EventWizardSession(
        position=int(value["position"]),
        starts_at=datetime.fromisoformat(str(value["starts_at"])),
        ends_at=datetime.fromisoformat(str(value["ends_at"])),
        local_label=str(value["local_label"]),
        join_url=raw_join_url or None,
    )


def _session_payload(session: EventWizardSession, *, join_url: str | None) -> dict[str, object]:
    return {
        "position": session.position,
        "starts_at": session.starts_at.isoformat(),
        "ends_at": session.ends_at.isoformat(),
        "local_label": session.local_label,
        "join_url": join_url,
    }


def _configured_sessions(data: dict[str, object]) -> tuple[EventWizardSession, ...]:
    raw = data.get("event_sessions") or []
    if not isinstance(raw, list):
        raise ValueError("invalid session collection")
    return tuple(_session_from_payload(item) for item in raw)


def _existing_edit_session(data: dict[str, object], position: int) -> dict[str, object]:
    raw = data.get("edit_existing_sessions") or []
    if not isinstance(raw, list):
        raise ValueError("invalid existing session collection")
    for item in raw:
        if isinstance(item, dict) and int(item.get("position") or 0) == position:
            return item
    raise ValueError("existing event session is unavailable")


async def _finish_schedule_edit(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    event_id = str(data.get("edit_event_id") or "")
    timezone_name = str(data.get("event_timezone") or "")
    sessions = _configured_sessions(data)
    if not business_id or not event_id or not timezone_name or not sessions:
        await state.clear()
        await message.answer("Не удалось сохранить расписание. Откройте вебинары заново.")
        return
    actor = await control._actor(int(message.from_user.id), business_id)
    try:
        specs = []
        for session in sessions:
            existing = _existing_edit_session(data, session.position)
            specs.append(
                EventSessionSpec(
                    starts_at=session.starts_at,
                    ends_at=session.ends_at,
                    join_url=str(existing.get("join_url") or "").strip() or None,
                    provider_key=str(existing.get("provider_key") or "").strip() or None,
                    provider_label=str(existing.get("provider_label") or "").strip() or None,
                )
            )
        updated = await asyncio.to_thread(
            configure_event_sessions,
            actor=actor,
            event_id=event_id,
            sessions=tuple(specs),
            reschedule_notifications=True,
        )
    except (KeyError, TypeError, ValueError):
        await message.answer(
            "Не удалось сохранить новое расписание. Проверьте, что все даты будущие и не пересекаются.",
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    except RuntimeError:
        await message.answer(
            "Не удалось сохранить новое расписание. Проверьте, что все даты будущие и не пересекаются.",
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    await state.clear()
    lines = "\n".join(
        f"День {session.position}: "
        + session.starts_at.astimezone(ZoneInfo(timezone_name)).strftime("%d.%m.%Y %H:%M")
        for session in updated
    )
    event_token = control._uuid_token(event_id)
    business_token = control._uuid_token(business_id)
    await message.answer(
        "✅ Расписание вебинара обновлено.\n\n" + lines,
        reply_markup=control._keyboard(
            [
                [("🗓 Контент-план", f"cpev:content:{event_token}:{business_token}")],
                [(BACK_TO_EVENTS_LABEL, f"cpev:home:{business_token}")],
            ]
        ),
    )


async def _store_edited_session_and_continue(
    message: Message,
    state: FSMContext,
    session: EventWizardSession,
) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    timezone_name = str(data.get("event_timezone") or "")
    total = int(data.get("event_days") or 0)
    position = int(data.get("event_session_index") or 0)
    existing = _existing_edit_session(data, position)
    configured = _configured_sessions(data)
    completed = EventWizardSession(
        position=session.position,
        starts_at=session.starts_at,
        ends_at=session.ends_at,
        local_label=session.local_label,
        join_url=str(existing.get("join_url") or "").strip() or None,
    )
    payloads = [_session_payload(item, join_url=item.join_url) for item in configured]
    payloads.append(_session_payload(completed, join_url=completed.join_url))
    await state.update_data(event_sessions=payloads)
    if position < total:
        next_position = position + 1
        await state.update_data(event_session_index=next_position, pending_session={})
        await _prompt_session_date(
            message,
            state,
            business_id=business_id,
            position=next_position,
            total=total,
            timezone_name=timezone_name,
        )
        return
    await _finish_schedule_edit(message, state)


def _mode_prompt(stage_label: str) -> str:
    return (
        f"Как оформить {stage_label}?\n\n"
        "1) Только текст\n"
        "2) Текст + картинка\n"
        "3) Текст в тематической картинке\n"
        "4) Текст + видео\n\n"
        "Можно ответить цифрой или названием варианта."
    )


async def _prompt_session_time(
    message: Message,
    state: FSMContext,
    *,
    business_id: str,
    position: int,
    total: int,
    timezone_name: str,
) -> None:
    await state.set_state(ClientPlatformEventLifecycleState.waiting_session_time)
    await message.answer(
        f"День {position} из {total}: когда эфир начинается и заканчивается?\n\n"
        "Напишите одной строкой, например: 25.09.2026 19:00-21:00\n"
        f"Часовой пояс: {timezone_name}.",
        reply_markup=_cancel_keyboard(business_id),
    )


async def _prompt_venue(
    message: Message,
    state: FSMContext,
    *,
    business_id: str,
    timezone_name: str,
) -> None:
    await state.update_data(event_timezone=timezone_name, event_session_index=1)
    await state.set_state(ClientPlatformEventLifecycleState.waiting_session_time)
    await message.answer(
        "Где будете проводить вебинар?\n\n"
        "Выберите площадку один раз — ClientPlatform будет открывать её для каждого дня. "
        "Ссылку не нужно печатать: после создания комнаты просто вернитесь и вставьте её из буфера.",
        reply_markup=_venue_keyboard(business_id),
    )


async def _prompt_session_date(
    message: Message,
    state: FSMContext,
    *,
    business_id: str,
    position: int,
    total: int,
    timezone_name: str,
) -> None:
    data = await state.get_data()
    minimum = local_today(timezone_name)
    configured = _configured_sessions(data)
    if configured:
        previous_date = configured[-1].starts_at.astimezone(ZoneInfo(timezone_name)).date()
        if previous_date > minimum:
            minimum = previous_date
    await state.update_data(
        event_picker_min_date=minimum.isoformat(),
        event_picker_month=month_key(minimum.year, minimum.month),
        event_picker_date="",
        event_picker_start="",
    )
    await state.set_state(ClientPlatformEventLifecycleState.waiting_session_time)
    await message.answer(
        f"День {position} из {total}: выберите дату.\nЧасовой пояс: {timezone_name}.",
        reply_markup=_calendar_keyboard(
            business_id=business_id,
            timezone_name=timezone_name,
            year=minimum.year,
            month=minimum.month,
            minimum_date=minimum,
        ),
    )


async def _prompt_session_url(
    message: Message,
    state: FSMContext,
    *,
    business_id: str,
    position: int,
    total: int,
    venue_key: str,
) -> None:
    venue = webinar_venue(venue_key)
    await state.set_state(ClientPlatformEventLifecycleState.waiting_session_url)
    later_note = (
        "Если ссылка появится позже — отправьте «-»; добавить её можно будет до эфира."
        if total == 1
        else "Для многодневного мероприятия у каждого дня должна быть своя комната."
    )
    open_note = (
        f"Нажмите «Открыть {venue.label}», создайте комнату, вернитесь сюда и вставьте скопированную HTTPS-ссылку."
        if venue.open_url
        else "Создайте комнату в выбранном сервисе и вставьте сюда её HTTPS-ссылку."
    )
    await message.answer(
        f"Комната для дня {position}.\n\n{open_note}\n{later_note}",
        reply_markup=_session_url_keyboard(business_id=business_id, venue_key=venue_key),
    )


async def _begin_warmup_choice(
    message: Message,
    state: FSMContext,
    *,
    actor,
    business_id: str,
    event_id: str,
    title: str,
    local_times: tuple[str, ...],
    registration_url: str,
    provider_key: str,
    join_ready: bool,
) -> None:
    window = await asyncio.to_thread(
        get_event_warmup_window,
        actor=actor,
        event_id=event_id,
    )
    await state.update_data(
        event_business_id=business_id,
        created_event_id=event_id,
        created_title=title,
        created_local_times=list(local_times),
        created_registration_url=registration_url,
        created_provider_key=provider_key,
        created_join_ready=join_ready,
        max_warmup_days=window.max_warmup_days,
    )
    await state.set_state(ClientPlatformEventLifecycleState.waiting_warmup_days)
    await message.answer(
        f"✅ Вебинар создан. До первого дня — {window.days_until_event} календ. дн.\n\n"
        f"Сколько дней готовить аудиторию сообщениями? Введите число от 0 до {window.max_warmup_days}.\n"
        f"Можно использовать весь доступный период — хоть все {window.max_warmup_days} дней до вебинара, "
        "по одному сообщению в день.\n"
        "0 — не отправлять сообщения до вебинара. ClientPlatform подготовит тексты как черновики владельца и ничего не разошлёт без разрешённого канала.",
        reply_markup=_cancel_keyboard(business_id),
    )


async def _store_mode(
    *,
    message: Message,
    data: dict[str, object],
    stage: EventContentStage,
    mode: EventContentMode,
) -> None:
    actor = await control._actor(
        int(message.from_user.id),
        str(data["event_business_id"]),
    )
    await asyncio.to_thread(
        set_event_content_mode,
        actor=actor,
        event_id=str(data["created_event_id"]),
        stage=stage,
        mode=mode,
    )


async def _ask_event_day_mode(message: Message, state: FSMContext, business_id: str) -> None:
    await state.set_state(ClientPlatformEventLifecycleState.waiting_event_day_mode)
    await message.answer(
        _mode_prompt("анонс в день мероприятия"),
        reply_markup=_cancel_keyboard(business_id),
    )


async def _ask_post_event_mode(message: Message, state: FSMContext, business_id: str) -> None:
    await state.set_state(ClientPlatformEventLifecycleState.waiting_post_event_mode)
    await message.answer(
        _mode_prompt("дожим после мероприятия"),
        reply_markup=_cancel_keyboard(business_id),
    )


async def _finish_content_setup(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    event_id = str(data.get("created_event_id") or "")
    if not business_id or not event_id:
        await state.clear()
        await message.answer("Настройки сохранены не полностью. Откройте вебинары заново.")
        return
    title = str(data.get("created_title") or "Вебинар")
    local_times = tuple(str(item) for item in data.get("created_local_times", []))
    registration_url = str(data.get("created_registration_url") or "")
    join_ready = bool(data.get("created_join_ready"))
    requested_days = int(data.get("warmup_requested_days") or 0)
    drafts_raw = data.get("warmup_drafts") or []
    drafts = tuple(item for item in drafts_raw if isinstance(item, dict))
    warmup_mode = EventContentMode(str(data.get("warmup_mode") or EventContentMode.TEXT.value))
    event_day_mode = EventContentMode(str(data.get("event_day_mode") or EventContentMode.TEXT.value))
    post_event_mode = EventContentMode(str(data.get("post_event_mode") or EventContentMode.TEXT.value))
    visual_requested = any(
        mode is not EventContentMode.TEXT
        for mode in (warmup_mode, event_day_mode, post_event_mode)
    )
    await state.clear()
    schedule = "\n".join(
        f"День {index}: {value}" for index, value in enumerate(local_times, start=1)
    )
    visual_note = (
        "\n\nДля выбранных визуальных режимов ClientPlatform использует общий генератор. "
        "Перед каждым платным AI-вызовом будет отдельное подтверждение — скрытых генераций не будет."
        if visual_requested
        else ""
    )
    await message.answer(
        f"✅ {title}\n{schedule}\n\nРегистрация: {registration_url}\n\n"
        f"Сообщения до вебинара: {requested_days} дн. — {event_content_mode_label(warmup_mode)}\n"
        f"В день мероприятия — {event_content_mode_label(event_day_mode)}\n"
        f"После мероприятия — {event_content_mode_label(post_event_mode)}."
        f"{visual_note}",
        reply_markup=control._keyboard(
            _event_actions(
                event_id=event_id,
                business_id=business_id,
                join_ready=join_ready,
                visual_requested=visual_requested,
            )
        ),
    )
    for draft in drafts:
        await message.answer(
            f"📨 Сообщение {int(draft['position'])}/{requested_days} — {draft['publish_date']}\n\n{draft['text']}"
        )


@router.callback_query(F.data.startswith("cpev:new:"))
async def start_multisession_event_wizard(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data or "").split(":", 2)[2]
    business_id = control._token_uuid(token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        actor.assert_can_manage_business()
    except TenantPermissionDenied:
        await callback.answer("Создавать мероприятия может владелец или администратор", show_alert=True)
        return
    await state.clear()
    await state.update_data(event_business_id=business_id)
    await state.set_state(ClientPlatformEventLifecycleState.waiting_title)
    await callback.answer()
    await control._callback_message(callback).answer(
        "🎥 Создаём вебинар\n\nКак называется мероприятие?",
        reply_markup=_cancel_keyboard(business_id),
    )


@router.callback_query(F.data.startswith("cpev:conduct:"))
async def open_webinar_live_room(callback: CallbackQuery) -> None:
    parts = str(callback.data or "").split(":", 3)
    if len(parts) != 4:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    event_id = control._token_uuid(parts[2])
    business_id = control._token_uuid(parts[3])
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        live = await asyncio.to_thread(
            resolve_event_live_snapshot,
            actor=actor,
            event_id=event_id,
        )
    except TenantPermissionDenied:
        await callback.answer("Не удалось открыть эфир этого вебинара", show_alert=True)
        return
    except LookupError:
        await callback.answer("Не удалось открыть эфир этого вебинара", show_alert=True)
        return
    except ValueError:
        await callback.answer("Не удалось открыть эфир этого вебинара", show_alert=True)
        return
    except RuntimeError:
        await callback.answer("Не удалось открыть эфир этого вебинара", show_alert=True)
        return
    ready = tuple(
        session
        for session in live.sessions
        if session.join_ready and session.join_url
    )
    if not ready:
        await callback.answer("Сначала добавьте ссылку на эфир", show_alert=True)
        return
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"▶️ Открыть день {session.position}"
                    if len(ready) > 1
                    else "▶️ Открыть эфир"
                ),
                url=session.join_url,
            )
        ]
        for session in ready
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=BACK_TO_EVENTS_LABEL,
                callback_data=f"cpev:home:{control._uuid_token(business_id)}",
            )
        ]
    )
    await callback.answer()
    schedule = "\n".join(
        f"День {session.position}: {session.local_start}"
        for session in ready
    )
    await control._callback_message(callback).answer(
        f"▶️ {live.title}\n\n{schedule}\n\nВыберите нужный эфир:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("cpev:edit:"))
async def start_schedule_edit(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data or "").split(":", 3)
    if len(parts) != 4:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    event_id = control._token_uuid(parts[2])
    business_id = control._token_uuid(parts[3])
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        actor.assert_can_manage_business()
        sessions, window = await asyncio.gather(
            asyncio.to_thread(list_event_sessions, actor=actor, event_id=event_id),
            asyncio.to_thread(get_event_warmup_window, actor=actor, event_id=event_id),
        )
        if not sessions:
            raise ValueError("event sessions are unavailable")
    except (TenantPermissionDenied, LookupError, ValueError):
        await callback.answer("Не удалось открыть расписание этого вебинара", show_alert=True)
        return
    except RuntimeError:
        await callback.answer("Не удалось открыть расписание этого вебинара", show_alert=True)
        return
    existing = [
        {
            "position": session.position,
            "join_url": session.join_url,
            "provider_key": session.provider_key,
            "provider_label": session.provider_label,
        }
        for session in sessions
    ]
    await state.clear()
    await state.update_data(
        event_business_id=business_id,
        edit_event_id=event_id,
        event_days=len(sessions),
        event_timezone=window.timezone_name,
        event_sessions=[],
        event_session_index=1,
        edit_existing_sessions=existing,
    )
    await callback.answer()
    await _prompt_session_date(
        control._callback_message(callback),
        state,
        business_id=business_id,
        position=1,
        total=len(sessions),
        timezone_name=window.timezone_name,
    )


@router.message(ClientPlatformEventLifecycleState.waiting_title)
async def receive_title(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    if not business_id:
        await state.clear()
        await message.answer("Не удалось продолжить. Откройте вебинары заново.")
        return
    if _is_cancel(message):
        await _cancel(message, state, business_id)
        return
    title = _normalized_text(message)
    if not title:
        await message.answer("Введите название вебинара.", reply_markup=_cancel_keyboard(business_id))
        return
    await state.update_data(event_title=title)
    await state.set_state(ClientPlatformEventLifecycleState.waiting_days)
    await message.answer(
        f"Сколько дней/эфиров будет в мероприятии?\n\nВведите число от 1 до {MAX_EVENT_SESSIONS}.",
        reply_markup=_cancel_keyboard(business_id),
    )


@router.message(ClientPlatformEventLifecycleState.waiting_days)
async def receive_days(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    if not business_id:
        await state.clear()
        await message.answer("Не удалось продолжить. Откройте вебинары заново.")
        return
    if _is_cancel(message):
        await _cancel(message, state, business_id)
        return
    try:
        days = parse_session_count(_normalized_text(message))
    except ValueError:
        await message.answer(
            f"Введите число от 1 до {MAX_EVENT_SESSIONS}.",
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    await state.update_data(
        event_days=days,
        event_sessions=[],
        event_session_index=1,
        event_topics=[],
    )
    await state.set_state(ClientPlatformEventLifecycleState.waiting_topics_choice)
    await message.answer(
        "У каждого дня вебинара есть своё название темы?\n\n"
        "Если да — ClientPlatform попросит названия и будет использовать их "
        "в сообщениях до вебинара.",
        reply_markup=_topics_keyboard(business_id),
    )


async def _prompt_event_timezone(message: Message, state: FSMContext, business_id: str) -> None:
    await state.set_state(ClientPlatformEventLifecycleState.waiting_timezone)
    await message.answer(
        "По какому времени идут эфиры?\n\n"
        "Нажмите «Москва» или «Другое время». При необходимости часовой пояс можно ввести вручную.",
        reply_markup=_timezone_keyboard(business_id),
    )


@router.callback_query(
    ClientPlatformEventLifecycleState.waiting_topics_choice,
    F.data == "cpev:topics:no",
)
async def choose_common_event_topic(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    if not business_id:
        await callback.answer("Мастер устарел. Откройте вебинары заново.", show_alert=True)
        return
    await state.update_data(event_topics=[])
    await callback.answer()
    await _prompt_event_timezone(control._callback_message(callback), state, business_id)


@router.callback_query(
    ClientPlatformEventLifecycleState.waiting_topics_choice,
    F.data == "cpev:topics:yes",
)
async def choose_named_event_topics(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    days = int(data.get("event_days") or 0)
    if not business_id or days < 1:
        await callback.answer("Мастер устарел. Откройте вебинары заново.", show_alert=True)
        return
    await state.set_state(ClientPlatformEventLifecycleState.waiting_topics)
    await callback.answer()
    await control._callback_message(callback).answer(
        f"Пришлите {days} названий тем — каждое с новой строки.\n\n"
        "Например:\nКак найти свою главную проблему\nЧто мешает изменениям\nПлан действий",
        reply_markup=_cancel_keyboard(business_id),
    )


@router.message(ClientPlatformEventLifecycleState.waiting_topics)
async def receive_event_topics(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    days = int(data.get("event_days") or 0)
    if not business_id or days < 1:
        await state.clear()
        await message.answer("Не удалось продолжить. Откройте вебинары заново.")
        return
    if _is_cancel(message):
        await _cancel(message, state, business_id)
        return
    topics = [
        " ".join(line.split()).strip()
        for line in str(message.text or "").splitlines()
        if line.strip()
    ]
    if len(topics) != days or any(len(topic) > 180 for topic in topics):
        await message.answer(
            f"Нужно ровно {days} названий, каждое с новой строки и до 180 символов.",
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    await state.update_data(event_topics=topics)
    await _prompt_event_timezone(message, state, business_id)


async def _accept_timezone(message: Message, state: FSMContext, timezone_value: str) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    days = int(data.get("event_days") or 0)
    if not business_id or days < 1:
        await state.clear()
        await message.answer("Не удалось продолжить. Откройте вебинары заново.")
        return
    try:
        timezone_name = normalize_event_timezone(timezone_value)
    except ValueError:
        await message.answer(
            "Не удалось определить часовой пояс. Напишите «Москва» или IANA-зону, например Europe/Amsterdam.",
            reply_markup=_timezone_keyboard(business_id),
        )
        return
    await _prompt_venue(
        message,
        state,
        business_id=business_id,
        timezone_name=timezone_name,
    )


@router.callback_query(
    ClientPlatformEventLifecycleState.waiting_timezone,
    F.data == "cpev:tz:moscow",
)
async def choose_moscow_timezone(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _accept_timezone(control._callback_message(callback), state, "Москва")


@router.callback_query(
    ClientPlatformEventLifecycleState.waiting_timezone,
    F.data == "cpev:tz:other",
)
async def choose_other_timezone(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    await callback.answer()
    await control._callback_message(callback).answer(
        "Напишите ваш часовой пояс, например Europe/Amsterdam или Asia/Yekaterinburg.",
        reply_markup=_timezone_keyboard(business_id) if business_id else None,
    )


@router.message(ClientPlatformEventLifecycleState.waiting_timezone)
async def receive_timezone(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    if not business_id:
        await state.clear()
        await message.answer("Не удалось продолжить. Откройте вебинары заново.")
        return
    if _is_cancel(message):
        await _cancel(message, state, business_id)
        return
    await _accept_timezone(message, state, _normalized_text(message))


@router.callback_query(
    ClientPlatformEventLifecycleState.waiting_session_time,
    F.data.startswith("cpev:venue:"),
)
async def choose_webinar_venue(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    timezone_name = str(data.get("event_timezone") or "")
    total = int(data.get("event_days") or 0)
    position = int(data.get("event_session_index") or 1)
    key = str(callback.data or "").split(":", 2)[2]
    try:
        venue = webinar_venue(key)
    except ValueError:
        await callback.answer("Неизвестная площадка", show_alert=True)
        return
    if not venue.public_room_supported:
        await callback.answer(venue.note, show_alert=True)
        return
    if not business_id or not timezone_name or total < 1:
        await callback.answer("Мастер устарел. Откройте вебинары заново.", show_alert=True)
        return
    await state.update_data(event_platform=venue.key)
    await callback.answer()
    await _prompt_session_date(
        control._callback_message(callback),
        state,
        business_id=business_id,
        position=position,
        total=total,
        timezone_name=timezone_name,
    )


@router.callback_query(F.data == "cpev:noop")
async def ignore_event_picker_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(
    ClientPlatformEventLifecycleState.waiting_session_time,
    F.data.startswith("cpev:month:"),
)
async def choose_calendar_month(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    timezone_name = str(data.get("event_timezone") or "")
    try:
        minimum = parse_calendar_date(
            str(data.get("event_picker_min_date") or ""),
            minimum=local_today(timezone_name),
        )
        year, month = parse_month_key(str(callback.data or "").split(":", 2)[2])
        calendar_days(year=year, month=month, minimum=minimum)
    except (IndexError, ValueError):
        await callback.answer("Этот месяц недоступен", show_alert=True)
        return
    selected_month = month_key(year, month)
    if str(data.get("event_picker_month") or "") == selected_month:
        await callback.answer()
        return
    await state.update_data(event_picker_month=selected_month)
    await callback.answer()
    await control._callback_message(callback).edit_reply_markup(
        reply_markup=_calendar_keyboard(
            business_id=business_id,
            timezone_name=timezone_name,
            year=year,
            month=month,
            minimum_date=minimum,
        )
    )


@router.callback_query(
    ClientPlatformEventLifecycleState.waiting_session_time,
    F.data.startswith("cpev:date:"),
)
async def choose_calendar_date(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    timezone_name = str(data.get("event_timezone") or "")
    try:
        minimum = parse_calendar_date(
            str(data.get("event_picker_min_date") or ""),
            minimum=local_today(timezone_name),
        )
        selected = parse_calendar_date(
            str(callback.data or "").split(":", 2)[2],
            minimum=minimum,
        )
    except (IndexError, ValueError):
        await callback.answer("Эта дата недоступна", show_alert=True)
        return
    selected_iso = selected.isoformat()
    if str(data.get("event_picker_date") or "") == selected_iso:
        await callback.answer()
        return
    await state.update_data(event_picker_date=selected_iso, event_picker_start="")
    await callback.answer()
    await control._callback_message(callback).answer(
        f"Дата: {selected.strftime('%d.%m.%Y')}. Во сколько начинаем?",
        reply_markup=_start_time_keyboard(business_id),
    )


@router.callback_query(
    ClientPlatformEventLifecycleState.waiting_session_time,
    F.data.startswith("cpev:start:"),
)
async def choose_session_start(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    raw = str(callback.data or "").split(":", 2)[2]
    if len(raw) != 4 or not raw.isdigit():
        await callback.answer("Время недоступно", show_alert=True)
        return
    value = f"{raw[:2]}:{raw[2:]}"
    try:
        start_time = parse_quick_time(value)
    except ValueError:
        await callback.answer("Время недоступно", show_alert=True)
        return
    await state.update_data(event_picker_start=start_time)
    await callback.answer()
    await control._callback_message(callback).answer(
        f"Начало: {start_time}. Сколько длится эфир?",
        reply_markup=_duration_keyboard(business_id),
    )


@router.callback_query(
    ClientPlatformEventLifecycleState.waiting_session_time,
    F.data.startswith("cpev:duration:"),
)
async def choose_session_duration(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    timezone_name = str(data.get("event_timezone") or "")
    total = int(data.get("event_days") or 0)
    position = int(data.get("event_session_index") or 0)
    venue_key = str(data.get("event_platform") or "other")
    callback_parts = str(callback.data or "").split(":", 2)
    if len(callback_parts) != 3:
        await callback.answer("Не удалось собрать время эфира. Выберите дату заново.", show_alert=True)
        return
    duration_token = callback_parts[2]
    try:
        minimum = parse_calendar_date(
            str(data.get("event_picker_min_date") or ""),
            minimum=local_today(timezone_name),
        )
        selected = parse_calendar_date(
            str(data.get("event_picker_date") or ""),
            minimum=minimum,
        )
        start_time = parse_quick_time(data.get("event_picker_start"))
        duration = parse_quick_duration(duration_token)
        session = parse_session_window(
            session_window_text(
                selected_date=selected,
                start_time=start_time,
                duration_minutes=duration,
            ),
            timezone_name=timezone_name,
            position=position,
        )
        configured = _configured_sessions(data)
        validate_session_sequence(session, previous=configured[-1] if configured else None)
    except (KeyError, TypeError, ValueError):
        await callback.answer("Не удалось собрать время эфира. Выберите дату заново.", show_alert=True)
        return
    if str(data.get("edit_event_id") or ""):
        await callback.answer()
        await _store_edited_session_and_continue(
            control._callback_message(callback),
            state,
            session,
        )
        return
    await state.update_data(
        pending_session={
            "position": session.position,
            "starts_at": session.starts_at.isoformat(),
            "ends_at": session.ends_at.isoformat(),
            "local_label": session.local_label,
        }
    )
    await callback.answer()
    await _prompt_session_url(
        control._callback_message(callback),
        state,
        business_id=business_id,
        position=position,
        total=total,
        venue_key=venue_key,
    )


@router.callback_query(
    ClientPlatformEventLifecycleState.waiting_session_time,
    F.data == "cpev:manual-time",
)
async def choose_manual_session_time(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    timezone_name = str(data.get("event_timezone") or "")
    total = int(data.get("event_days") or 0)
    position = int(data.get("event_session_index") or 0)
    await callback.answer()
    await _prompt_session_time(
        control._callback_message(callback),
        state,
        business_id=business_id,
        position=position,
        total=total,
        timezone_name=timezone_name,
    )


@router.message(ClientPlatformEventLifecycleState.waiting_session_time)
async def receive_session_time(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    timezone_name = str(data.get("event_timezone") or "")
    total = int(data.get("event_days") or 0)
    position = int(data.get("event_session_index") or 0)
    venue_key = str(data.get("event_platform") or "other")
    if not business_id or not timezone_name or total < 1 or position < 1 or position > total:
        await state.clear()
        await message.answer("Не удалось продолжить. Откройте вебинары заново.")
        return
    if _is_cancel(message):
        await _cancel(message, state, business_id)
        return
    try:
        session = parse_session_window(
            _normalized_text(message),
            timezone_name=timezone_name,
            position=position,
        )
        previous_sessions = _configured_sessions(data)
        previous = previous_sessions[-1] if previous_sessions else None
        validate_session_sequence(session, previous=previous)
    except (KeyError, TypeError, ValueError):
        await message.answer(
            "Не удалось понять интервал. Напишите дату, начало и окончание, например: 25.09.2026 19:00-21:00. Следующий эфир не должен пересекаться с предыдущим.",
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    if str(data.get("edit_event_id") or ""):
        await _store_edited_session_and_continue(message, state, session)
        return
    await state.update_data(
        pending_session={
            "position": session.position,
            "starts_at": session.starts_at.isoformat(),
            "ends_at": session.ends_at.isoformat(),
            "local_label": session.local_label,
        }
    )
    await _prompt_session_url(
        message,
        state,
        business_id=business_id,
        position=position,
        total=total,
        venue_key=venue_key,
    )


async def _create_configured_event(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    title = str(data.get("event_title") or "").strip()
    timezone_name = str(data.get("event_timezone") or "")
    if not business_id or not title or not timezone_name:
        await state.clear()
        await message.answer("Не удалось завершить создание. Откройте вебинары заново.")
        return
    try:
        sessions = _configured_sessions(data)
    except (KeyError, TypeError, ValueError):
        sessions = ()
    expected_count = int(data.get("event_days") or 0)
    if len(sessions) != expected_count or not sessions:
        await message.answer(
            "Не удалось завершить создание: заполнены не все дни.",
            reply_markup=_cancel_keyboard(business_id),
        )
        return

    topics = tuple(
        str(item).strip()
        for item in (data.get("event_topics") or [])
        if str(item).strip()
    )
    description = (
        "Программа по дням:\n"
        + "\n".join(f"День {index}: {topic}" for index, topic in enumerate(topics, start=1))
        if topics
        else ""
    )
    actor = await control._actor(int(message.from_user.id), business_id)
    try:
        if len(sessions) == 1:
            session = sessions[0]
            created = await asyncio.to_thread(
                create_and_publish_online_event,
                actor=actor,
                request=OnlineEventCreateRequest(
                    title=title,
                    starts_at=session.starts_at,
                    ends_at=session.ends_at,
                    timezone_name=timezone_name,
                    join_url=session.join_url,
                    description=description,
                ),
            )
        else:
            created = await asyncio.to_thread(
                create_and_publish_multisession_online_event,
                actor=actor,
                request=MultiSessionOnlineEventCreateRequest(
                    title=title,
                    timezone_name=timezone_name,
                    description=description,
                    sessions=tuple(
                        OnlineEventSessionCreateRequest(
                            starts_at=session.starts_at,
                            ends_at=session.ends_at,
                            join_url=session.join_url,
                        )
                        for session in sessions
                    ),
                ),
            )
        registration_url = created.registration_url(_public_base_url())
        await _begin_warmup_choice(
            message,
            state,
            actor=actor,
            business_id=business_id,
            event_id=created.event_id,
            title=title,
            local_times=tuple(
                (
                    f"{session.local_label} — {topics[index]}"
                    if index < len(topics)
                    else session.local_label
                )
                for index, session in enumerate(sessions)
            ),
            registration_url=registration_url,
            provider_key=created.provider_key,
            join_ready=created.join_ready,
        )
    except (ValueError, RuntimeError):
        await message.answer(
            "Не удалось создать вебинар. Проверьте даты, время окончания и HTTPS-ссылки.",
            reply_markup=_cancel_keyboard(business_id),
        )


@router.message(ClientPlatformEventLifecycleState.waiting_session_url)
async def receive_session_url(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    total = int(data.get("event_days") or 0)
    position = int(data.get("event_session_index") or 0)
    timezone_name = str(data.get("event_timezone") or "")
    venue_key = str(data.get("event_platform") or "other")
    if not business_id or total < 1 or position < 1 or position > total or not timezone_name:
        await state.clear()
        await message.answer("Не удалось продолжить. Откройте вебинары заново.")
        return
    if _is_cancel(message):
        await _cancel(message, state, business_id)
        return
    try:
        configured = _configured_sessions(data)
        pending = _session_from_payload(data.get("pending_session"))
        existing_urls = tuple(
            session.join_url for session in configured if session.join_url is not None
        )
        join_url = normalize_session_join_url(
            str(message.text or "").strip(),
            existing_urls=existing_urls,
        )
        if total > 1 and join_url is None:
            raise ValueError("multi-session event requires a room for every session")
    except (KeyError, TypeError, ValueError) as exc:
        text = str(exc)
        if "own room" in text:
            answer = "Для каждого дня нужна своя ссылка на комнату. Эта ссылка уже используется другим днём."
        elif "requires a room" in text:
            answer = "Для многодневного мероприятия нужна отдельная HTTPS-ссылка на комнату каждого дня."
        else:
            answer = (
                "Ссылка должна начинаться с https://. Если её пока нет — отправьте «-»."
                if total == 1
                else "Ссылка должна начинаться с https:// и быть отдельной для этого дня."
            )
        await message.answer(
            answer,
            reply_markup=_session_url_keyboard(business_id=business_id, venue_key=venue_key),
        )
        return

    completed = EventWizardSession(
        position=pending.position,
        starts_at=pending.starts_at,
        ends_at=pending.ends_at,
        local_label=pending.local_label,
        join_url=join_url,
    )
    payloads = [
        _session_payload(session, join_url=session.join_url) for session in configured
    ]
    payloads.append(_session_payload(completed, join_url=join_url))
    await state.update_data(event_sessions=payloads)

    if position < total:
        next_position = position + 1
        await state.update_data(event_session_index=next_position, pending_session={})
        await _prompt_session_date(
            message,
            state,
            business_id=business_id,
            position=next_position,
            total=total,
            timezone_name=timezone_name,
        )
        return

    await _create_configured_event(message, state)


@router.message(ClientPlatformEventLifecycleState.waiting_warmup_days)
async def receive_warmup_days(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    event_id = str(data.get("created_event_id") or "")
    if not business_id or not event_id:
        await state.clear()
        await message.answer("Вебинар создан, но не удалось продолжить настройку сообщений. Откройте вебинары заново.")
        return
    if _is_cancel(message):
        await state.clear()
        await message.answer(
            "Вебинар уже создан. Настройка сообщений до вебинара остановлена.",
            reply_markup=control._keyboard(
                _event_actions(
                    event_id=event_id,
                    business_id=business_id,
                    join_ready=bool(data.get("created_join_ready")),
                )
            ),
        )
        return
    try:
        requested_days = int(_normalized_text(message))
    except ValueError:
        requested_days = -1
    maximum = int(data.get("max_warmup_days") or 0)
    if requested_days < 0 or requested_days > maximum:
        await message.answer(
            f"Введите число от 0 до {maximum}.",
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    actor = await control._actor(int(message.from_user.id), business_id)
    try:
        plan = await asyncio.to_thread(
            save_event_warmup_plan,
            actor=actor,
            event_id=event_id,
            requested_days=requested_days,
        )
    except (ValueError, RuntimeError):
        await message.answer(
            "Не удалось подготовить сообщения. Попробуйте другое число дней.",
            reply_markup=_cancel_keyboard(business_id),
        )
        return

    await state.update_data(
        warmup_requested_days=plan.requested_days,
        warmup_drafts=[
            {
                "position": int(draft.position),
                "publish_date": draft.publish_date.strftime("%d.%m.%Y"),
                "text": str(draft.text),
            }
            for draft in plan.drafts
        ],
    )
    if plan.requested_days == 0:
        await asyncio.to_thread(
            set_event_content_mode,
            actor=actor,
            event_id=event_id,
            stage=EventContentStage.WARMUP,
            mode=EventContentMode.TEXT,
        )
        await state.update_data(warmup_mode=EventContentMode.TEXT.value)
        await _ask_event_day_mode(message, state, business_id)
        return
    await state.set_state(ClientPlatformEventLifecycleState.waiting_warmup_mode)
    await message.answer(
        _mode_prompt("сообщения до вебинара"),
        reply_markup=_cancel_keyboard(business_id),
    )


@router.message(ClientPlatformEventLifecycleState.waiting_warmup_mode)
async def receive_warmup_mode(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    if not business_id or not data.get("created_event_id"):
        await state.clear()
        await message.answer("Не удалось продолжить. Откройте вебинары заново.")
        return
    if _is_cancel(message):
        await _cancel(message, state, business_id)
        return
    try:
        mode = parse_event_content_mode(_normalized_text(message))
        await _store_mode(message=message, data=data, stage=EventContentStage.WARMUP, mode=mode)
    except (KeyError, ValueError, RuntimeError):
        await message.answer(_mode_prompt("сообщения до вебинара"), reply_markup=_cancel_keyboard(business_id))
        return
    await state.update_data(warmup_mode=mode.value)
    await _ask_event_day_mode(message, state, business_id)


@router.message(ClientPlatformEventLifecycleState.waiting_event_day_mode)
async def receive_event_day_mode(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    if not business_id or not data.get("created_event_id"):
        await state.clear()
        await message.answer("Не удалось продолжить. Откройте вебинары заново.")
        return
    if _is_cancel(message):
        await _cancel(message, state, business_id)
        return
    try:
        mode = parse_event_content_mode(_normalized_text(message))
        await _store_mode(message=message, data=data, stage=EventContentStage.EVENT_DAY, mode=mode)
    except (KeyError, ValueError, RuntimeError):
        await message.answer(
            _mode_prompt("анонс в день мероприятия"),
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    await state.update_data(event_day_mode=mode.value)
    await _ask_post_event_mode(message, state, business_id)


@router.message(ClientPlatformEventLifecycleState.waiting_post_event_mode)
async def receive_post_event_mode(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    if not business_id or not data.get("created_event_id"):
        await state.clear()
        await message.answer("Не удалось продолжить. Откройте вебинары заново.")
        return
    if _is_cancel(message):
        await _cancel(message, state, business_id)
        return
    try:
        mode = parse_event_content_mode(_normalized_text(message))
        await _store_mode(message=message, data=data, stage=EventContentStage.POST_EVENT, mode=mode)
    except (KeyError, ValueError, RuntimeError):
        await message.answer(
            _mode_prompt("дожим после мероприятия"),
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    await state.update_data(post_event_mode=mode.value)
    await _finish_content_setup(message, state)


__all__ = ["ClientPlatformEventLifecycleState", "router"]

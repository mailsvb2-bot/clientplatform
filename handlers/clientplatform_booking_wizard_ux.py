from __future__ import annotations

import asyncio
import importlib
from datetime import date, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, User

from clientplatform.application.activity import get_business_profile
from clientplatform.presentation.event_schedule_picker import (
    MAX_CALENDAR_MONTHS,
    calendar_days,
    local_today,
    month_distance,
    month_key,
    parse_month_key,
    shift_month,
)

control = importlib.import_module(".clientplatform_control", __package__)


def _owner_module():
    # Lazy import keeps this UX module import-order independent: owner_journey
    # imports the canonical entry router, which composes this router.
    return importlib.import_module(".clientplatform_owner_journey", __package__)


router = Router(name="clientplatform_booking_wizard_ux")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

_QUICK_DURATIONS = (15, 30, 45, 60, 75, 90, 120, 180)
_BOOKING_START_TIMES = tuple(
    f"{hour:02d}:{minute:02d}"
    for hour in range(8, 22)
    for minute in (0, 30)
) + ("22:00",)
# Kept for callback compatibility with keyboards rendered by earlier releases.
_DATE_PAGE_SIZE = 7
_MAX_DATE_DAYS = 365
_MONTHS_RU = ("", "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
_MONTHS_RU_FULL = (
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
_WEEKDAYS_RU = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def _business_token(business_id: str) -> str:
    return control._uuid_token(business_id)


def _date_label(value: date) -> str:
    return f"{value.day} {_MONTHS_RU[value.month]}"


def _date_keyboard(
    business_id: str,
    *,
    minimum: date,
    year: int | None = None,
    month: int | None = None,
    offset: int = 0,
):
    """Render a real month calendar while keeping legacy offset callbacks usable."""

    token = _business_token(business_id)
    if year is None or month is None:
        target = minimum + timedelta(days=max(0, offset))
        year, month = target.year, target.month
    # Shared validation keeps booking and webinar calendars on one month-window contract.
    weeks = calendar_days(
        year=year,
        month=month,
        minimum=minimum,
        max_months=MAX_CALENDAR_MONTHS,
    )
    rows: list[list[tuple[str, str]]] = [
        [(f"{_MONTHS_RU_FULL[month]} {year}", f"cpj:wiznoop:{token}")],
        [(label, f"cpj:wiznoop:{token}") for label in _WEEKDAYS_RU],
    ]
    for week in weeks:
        rows.append(
            [
                (
                    str(cell.day) if cell.day else "·",
                    (
                        f"cpj:wizdate:{token}:{cell.value}"
                        if cell.enabled and cell.value
                        else f"cpj:wiznoop:{token}"
                    ),
                )
                for cell in week
            ]
        )

    distance = month_distance(minimum, year=year, month=month)
    navigation: list[tuple[str, str]] = []
    if distance > 0:
        previous_year, previous_month = shift_month(year, month, -1)
        navigation.append(
            (
                f"⬅️ {_MONTHS_RU_FULL[previous_month]}",
                f"cpj:wizmonth:{token}:{month_key(previous_year, previous_month)}",
            )
        )
    if distance < MAX_CALENDAR_MONTHS:
        next_year, next_month = shift_month(year, month, 1)
        navigation.append(
            (
                f"{_MONTHS_RU_FULL[next_month]} ➡️",
                f"cpj:wizmonth:{token}:{month_key(next_year, next_month)}",
            )
        )
    if navigation:
        rows.append(navigation)
    rows.append([("✖️ Отмена", f"cpj:wizcancel:{token}")])
    return control._keyboard(rows)


def _time_keyboard(business_id: str):
    token = _business_token(business_id)
    rows = [
        [
            (value, f"cpj:wiztime:{token}:{value.replace(':', '')}")
            for value in _BOOKING_START_TIMES[index : index + 3]
        ]
        for index in range(0, len(_BOOKING_START_TIMES), 3)
    ]
    rows.append([("✖️ Отмена", f"cpj:wizcancel:{token}")])
    return control._keyboard(rows)


async def send_booking_date_picker(
    message: Message,
    state: FSMContext,
    *,
    business_id: str,
    timezone_name: str,
    heading: str = "",
) -> None:
    minimum = local_today(timezone_name)
    await state.set_state(control.ClientPlatformControlState.booking_start)
    await state.update_data(
        booking_picker_timezone=timezone_name,
        booking_picker_min_date=minimum.isoformat(),
        booking_picker_month=month_key(minimum.year, minimum.month),
        booking_picker_date="",
    )
    prefix = f"{heading.rstrip()}\n\n" if heading.strip() else ""
    await message.answer(
        prefix
        + "Выберите дату свободного времени. Потом останется выбрать время и длительность кнопками.",
        reply_markup=_date_keyboard(business_id, minimum=minimum),
    )


def _duration_keyboard(business_id: str):
    token = _business_token(business_id)
    labels = {
        15: "15 мин",
        30: "30 мин",
        45: "45 мин",
        60: "1 час",
        75: "1 ч 15 мин",
        90: "1,5 часа",
        120: "2 часа",
        180: "3 часа",
    }
    rows = [
        [
            (labels[value], f"cpj:wizdur:{token}:{value}")
            for value in _QUICK_DURATIONS[index : index + 3]
        ]
        for index in range(0, len(_QUICK_DURATIONS), 3)
    ]
    rows.extend(
        [
            [("⬅️ Изменить дату и время", f"cpj:wizback:{token}")],
            [("✖️ Отмена", f"cpj:wizcancel:{token}")],
        ]
    )
    return control._keyboard(rows)


def _cancel_keyboard(business_id: str):
    token = _business_token(business_id)
    return control._keyboard(
        [
            [("⬅️ Изменить дату и время", f"cpj:wizback:{token}")],
            [("✖️ Отмена", f"cpj:wizcancel:{token}")],
        ]
    )


async def _state_business(
    callback: CallbackQuery,
    state: FSMContext,
    business_token: str,
) -> tuple[str, dict[str, Any]] | None:
    business_id = control._token_uuid(business_token)
    data = await state.get_data()
    if str(data.get("business_id") or "") != business_id:
        await callback.answer("Этот шаг уже устарел. Откройте кабинет заново.", show_alert=True)
        return None
    await control._actor(int(callback.from_user.id), business_id)
    return business_id, data


async def _remove_keyboard(message: Message) -> None:
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        return


class _DurationMessageProxy:
    """Reuse the canonical booking completion handler without duplicating domain logic."""

    def __init__(self, message: Message, user: User, duration: int) -> None:
        self._message = message
        self.from_user = user
        self.text = str(duration)

    async def answer(self, text: str, **kwargs: Any):
        return await self._message.answer(text, **kwargs)


@router.callback_query(F.data.startswith("cpj:wiznoop:"))
async def ignore_booking_calendar_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("cpj:wizmonth:"))
async def choose_booking_month(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, raw_month = str(callback.data).split(":", 3)
    resolved = await _state_business(callback, state, business_token)
    if resolved is None:
        return
    business_id, data = resolved
    try:
        minimum = date.fromisoformat(str(data["booking_picker_min_date"]))
        year, month = parse_month_key(raw_month)
        calendar_days(
            year=year,
            month=month,
            minimum=minimum,
            max_months=MAX_CALENDAR_MONTHS,
        )
    except (KeyError, ValueError):
        await callback.answer("Этот месяц недоступен", show_alert=True)
        return
    selected_month = month_key(year, month)
    if str(data.get("booking_picker_month") or "") == selected_month:
        await callback.answer()
        return
    await state.update_data(booking_picker_month=selected_month)
    await callback.answer()
    await control._callback_message(callback).edit_reply_markup(
        reply_markup=_date_keyboard(
            business_id,
            minimum=minimum,
            year=year,
            month=month,
        )
    )


@router.callback_query(F.data.startswith("cpj:wizdatepage:"))
async def choose_booking_date_page(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, raw_offset = str(callback.data).split(":", 3)
    resolved = await _state_business(callback, state, business_token)
    if resolved is None:
        return
    business_id, data = resolved
    try:
        offset = int(raw_offset)
        if offset < 0 or offset > _MAX_DATE_DAYS:
            raise ValueError("date offset out of range")
        minimum = date.fromisoformat(str(data["booking_picker_min_date"]))
    except (KeyError, ValueError):
        await callback.answer("Выбор даты устарел. Откройте услугу заново.", show_alert=True)
        return
    await callback.answer()
    target = minimum + timedelta(days=offset)
    await state.update_data(booking_picker_month=month_key(target.year, target.month))
    await control._callback_message(callback).edit_reply_markup(
        reply_markup=_date_keyboard(
            business_id,
            minimum=minimum,
            year=target.year,
            month=target.month,
        )
    )


@router.callback_query(F.data.startswith("cpj:wizdate:"))
async def choose_booking_date(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, raw_date = str(callback.data).split(":", 3)
    resolved = await _state_business(callback, state, business_token)
    if resolved is None:
        return
    business_id, data = resolved
    try:
        selected = date.fromisoformat(raw_date)
        minimum = date.fromisoformat(str(data["booking_picker_min_date"]))
        calendar_days(
            year=selected.year,
            month=selected.month,
            minimum=minimum,
            max_months=MAX_CALENDAR_MONTHS,
        )
        if selected < minimum:
            raise ValueError("date outside range")
    except (KeyError, ValueError):
        await callback.answer("Эта дата недоступна", show_alert=True)
        return
    await state.update_data(booking_picker_date=selected.isoformat())
    await callback.answer()
    await control._callback_message(callback).answer(
        f"Дата: {selected.strftime('%d.%m.%Y')}. Во сколько начинаем?",
        reply_markup=_time_keyboard(business_id),
    )


@router.callback_query(F.data.startswith("cpj:wiztime:"))
async def choose_booking_time(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, raw_time = str(callback.data).split(":", 3)
    resolved = await _state_business(callback, state, business_token)
    if resolved is None:
        return
    business_id, data = resolved
    try:
        if len(raw_time) != 4 or not raw_time.isdigit():
            raise ValueError("invalid time")
        value = f"{raw_time[:2]}:{raw_time[2:]}"
        if value not in _BOOKING_START_TIMES:
            raise ValueError("unsupported booking start time")
        selected = date.fromisoformat(str(data["booking_picker_date"]))
    except (KeyError, ValueError):
        await callback.answer("Выберите дату и время заново", show_alert=True)
        return
    local_start = f"{selected.strftime('%d.%m.%Y')} {value}"
    await state.update_data(booking_start=local_start)
    await state.set_state(control.ClientPlatformControlState.booking_duration)
    prefix = "Новое время принято." if data.get("replacing_slot_id") else "Дата и время приняты."
    await callback.answer()
    await control._callback_message(callback).answer(
        f"{prefix} Выберите длительность — обычно достаточно одного нажатия.",
        reply_markup=_duration_keyboard(business_id),
    )


@router.callback_query(F.data.startswith("cpj:wizmanual:"))
async def choose_manual_booking_datetime(callback: CallbackQuery, state: FSMContext) -> None:
    """Upgrade legacy keyboards to the current button-only calendar flow."""

    business_token = str(callback.data).split(":", 2)[2]
    resolved = await _state_business(callback, state, business_token)
    if resolved is None:
        return
    business_id, data = resolved
    timezone_name = str(data.get("booking_picker_timezone") or "").strip()
    if not timezone_name:
        actor = await control._actor(int(callback.from_user.id), business_id)
        profile = await asyncio.to_thread(get_business_profile, actor=actor)
        timezone_name = profile.timezone
    await callback.answer("Дата и время теперь выбираются кнопками")
    await send_booking_date_picker(
        control._callback_message(callback),
        state,
        business_id=business_id,
        timezone_name=timezone_name,
        heading="Выберите дату и время кнопками.",
    )


@router.message(control.ClientPlatformControlState.booking_start)
async def receive_booking_start_with_quick_duration(
    message: Message,
    state: FSMContext,
) -> None:
    """Keep date/time selection button-only even for typed legacy input."""

    data = await state.get_data()
    business_id = str(data.get("business_id") or "")
    if not business_id:
        await state.clear()
        await message.answer("Не удалось продолжить настройку. Откройте кабинет через /start.")
        return
    timezone_name = str(data.get("booking_picker_timezone") or "").strip()
    if not timezone_name:
        actor = await control._actor(int(message.from_user.id), business_id)
        profile = await asyncio.to_thread(get_business_profile, actor=actor)
        timezone_name = profile.timezone
    await send_booking_date_picker(
        message,
        state,
        business_id=business_id,
        timezone_name=timezone_name,
        heading="Дата и время выбираются кнопками.",
    )


@router.message(control.ClientPlatformControlState.booking_duration)
async def reject_typed_booking_duration(message: Message, state: FSMContext) -> None:
    """Enforce the owner-visible duration contract as button-only."""

    data = await state.get_data()
    business_id = str(data.get("business_id") or "")
    if not business_id:
        await state.clear()
        await message.answer("Не удалось продолжить настройку. Откройте кабинет через /start.")
        return
    await message.answer(
        "Длительность выбирается кнопкой.",
        reply_markup=_duration_keyboard(business_id),
    )


@router.callback_query(
    StateFilter(control.ClientPlatformControlState.booking_duration),
    F.data.startswith("cpj:wizdur:"),
)
async def choose_quick_duration(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, raw_duration = str(callback.data).split(":", 3)
    duration = int(raw_duration)
    if duration not in _QUICK_DURATIONS:
        await callback.answer("Выберите длительность заново", show_alert=True)
        return
    resolved = await _state_business(callback, state, business_token)
    if resolved is None:
        return
    message = control._callback_message(callback)
    await _remove_keyboard(message)
    await callback.answer(f"{duration} минут")
    await _owner_module().receive_owner_booking_duration(
        _DurationMessageProxy(message, callback.from_user, duration),
        state,
    )


@router.callback_query(
    StateFilter(control.ClientPlatformControlState.booking_duration),
    F.data.startswith("cpj:wizcustom:"),
)
async def choose_custom_duration(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    resolved = await _state_business(callback, state, business_token)
    if resolved is None:
        return
    business_id, _data = resolved
    message = control._callback_message(callback)
    await _remove_keyboard(message)
    await callback.answer("Длительность теперь выбирается кнопками")
    await message.answer(
        "Выберите длительность встречи или услуги.",
        reply_markup=_duration_keyboard(business_id),
    )


@router.callback_query(
    StateFilter(control.ClientPlatformControlState.booking_duration),
    F.data.startswith("cpj:wizback:"),
)
async def return_to_booking_start(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    resolved = await _state_business(callback, state, business_token)
    if resolved is None:
        return
    business_id, _data = resolved
    actor = await control._actor(int(callback.from_user.id), business_id)
    profile = await asyncio.to_thread(get_business_profile, actor=actor)
    message = control._callback_message(callback)
    await _remove_keyboard(message)
    await callback.answer()
    await send_booking_date_picker(
        message,
        state,
        business_id=business_id,
        timezone_name=profile.timezone,
        heading="Выберите новые дату и время.",
    )


@router.callback_query(F.data.startswith("cpj:wizcancel:"))
async def cancel_booking_wizard(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    resolved = await _state_business(callback, state, business_token)
    if resolved is None:
        return
    business_id, _data = resolved
    message = control._callback_message(callback)
    await _remove_keyboard(message)
    await state.clear()
    await callback.answer("Настройка отменена")
    await _owner_module().send_owner_dashboard(
        message,
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


__all__ = [
    "cancel_booking_wizard",
    "choose_booking_date",
    "choose_booking_date_page",
    "choose_booking_month",
    "choose_booking_time",
    "choose_custom_duration",
    "choose_manual_booking_datetime",
    "choose_quick_duration",
    "ignore_booking_calendar_noop",
    "receive_booking_start_with_quick_duration",
    "return_to_booking_start",
    "router",
    "send_booking_date_picker",
]

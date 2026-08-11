from __future__ import annotations

"""Missing-schedule continuation for the goal-first owner journey.

When an owner asks for clients before any bookable time exists, keep the
conversation in business language: choose or create the service, ask only for
availability (and duration when it cannot be inferred), create the slot, then
resume the canonical one-click advertising orchestration automatically.
"""

import asyncio
import re
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from clientplatform.domain.activity import ActivityError, CapabilityStatus
from clientplatform.domain.bookings import BookingError, BookingSlotStatus
from clientplatform.domain.tenancy import TenantPermissionDenied

from . import clientplatform_control as control
from . import clientplatform_one_click_experience as one_click


router = Router(name="clientplatform_goal_schedule")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

_OFFERINGS_PAGE_SIZE = 8


class GoalScheduleState(StatesGroup):
    waiting_offering_title = State()
    waiting_booking_start = State()
    waiting_booking_duration = State()


@dataclass(slots=True)
class _ResumeCallback:
    """Minimal callback surface used to resume the existing one-click handler."""

    message: Message
    business_token: str

    @property
    def data(self) -> str:
        return f"cpo:start:{self.business_token}"

    @property
    def from_user(self):
        return self.message.from_user

    @property
    def bot(self):
        return self.message.bot

    async def answer(self, *_args, **_kwargs) -> None:
        # The original callback was already answered before the user entered
        # schedule data. Resume messages are ordinary Telegram messages.
        return None


async def _selectable_capabilities(actor):
    capabilities = await asyncio.to_thread(
        control.list_business_capabilities,
        actor=actor,
    )
    return [
        item
        for item in capabilities
        if item.status == CapabilityStatus.ACTIVE
        and item.connector_key in {"consultations", "services", "custom"}
    ]


async def _selectable_offerings(actor):
    usable = await _selectable_capabilities(actor)
    groups = await asyncio.gather(
        *[
            asyncio.to_thread(
                control.list_business_offerings,
                actor=actor,
                capability_id=capability.id,
            )
            for capability in usable
        ]
    )
    return [item for group in groups for item in group]


async def _show_offering_page(
    target: Message,
    *,
    business_token: str,
    offerings,
    page: int,
) -> None:
    total_pages = max(
        1,
        (len(offerings) + _OFFERINGS_PAGE_SIZE - 1) // _OFFERINGS_PAGE_SIZE,
    )
    safe_page = max(0, min(page, total_pages - 1))
    start = safe_page * _OFFERINGS_PAGE_SIZE
    current = offerings[start : start + _OFFERINGS_PAGE_SIZE]
    rows = [
        [
            (
                f"🎯 {item.title[:42]}",
                f"cpo:offer:{business_token}:{control._uuid_token(item.id)}",
            )
        ]
        for item in current
    ]
    navigation = []
    if safe_page > 0:
        navigation.append(
            ("⬅️ Назад", f"cpo:offers:{business_token}:{safe_page - 1}")
        )
    if safe_page + 1 < total_pages:
        navigation.append(
            ("Дальше ➡️", f"cpo:offers:{business_token}:{safe_page + 1}")
        )
    if navigation:
        rows.append(navigation)
    rows.append([("🏠 Отмена", f"cpj:home:{business_token}")])
    suffix = (
        f" · страница {safe_page + 1}/{total_pages}"
        if total_pages > 1
        else ""
    )
    await target.answer(
        f"Для какой услуги сейчас нужен новый клиент?{suffix}",
        reply_markup=control._keyboard(rows),
    )


async def _ask_booking_start(
    target: Message,
    state: FSMContext,
    *,
    business_id: str,
    business_token: str,
    offering,
) -> None:
    await state.set_state(GoalScheduleState.waiting_booking_start)
    await state.set_data(
        {
            "business_id": business_id,
            "business_token": business_token,
            "offering_id": str(offering.id),
            "offering_title": str(offering.title),
        }
    )
    await target.answer(
        f"Когда Вы можете принять нового клиента на «{offering.title}»?\n\n"
        "Напишите дату и время, например: 20.08.2026 12:00. "
        "Остальное ClientPlatform продолжит сама."
    )


async def _begin_missing_schedule(
    event: CallbackQuery,
    state: FSMContext,
    *,
    actor,
    business_id: str,
    business_token: str,
) -> None:
    target = control._callback_message(event)
    usable = await _selectable_capabilities(actor)
    if not usable:
        try:
            usable = [
                await asyncio.to_thread(
                    control.enable_business_capability,
                    actor=actor,
                    connector_key="services",
                )
            ]
        except (ActivityError, TenantPermissionDenied, ValueError):
            await target.answer(
                "Не получилось автоматически подготовить запись. Ничего опасного "
                "не изменено.",
                reply_markup=control._keyboard(
                    [[("🏠 В кабинет", f"cpj:home:{business_token}")]]
                ),
            )
            return

    groups = await asyncio.gather(
        *[
            asyncio.to_thread(
                control.list_business_offerings,
                actor=actor,
                capability_id=capability.id,
            )
            for capability in usable
        ]
    )
    offerings = [item for group in groups for item in group]
    if not offerings:
        await state.set_state(GoalScheduleState.waiting_offering_title)
        await state.set_data(
            {
                "business_id": business_id,
                "business_token": business_token,
                "capability_id": str(usable[0].id),
            }
        )
        await target.answer(
            "Как называется услуга, на которую Вы хотите получить клиента?\n\n"
            "Например: «Консультация», «Ремонт раковины» или «Занятие английским»."
        )
        return
    if len(offerings) == 1:
        await _ask_booking_start(
            target,
            state,
            business_id=business_id,
            business_token=business_token,
            offering=offerings[0],
        )
        return
    await state.clear()
    await _show_offering_page(
        target,
        business_token=business_token,
        offerings=offerings,
        page=0,
    )


async def _find_offering(actor, offering_id: str):
    offerings = await _selectable_offerings(actor)
    return next((item for item in offerings if str(item.id) == offering_id), None)


def _duration_from_title(title: str) -> int | None:
    match = re.search(r"\b([1-9][0-9]{0,2})\s*(?:мин|минут)", str(title).lower())
    if match is None:
        return None
    value = int(match.group(1))
    return value if 5 <= value <= 720 else None


async def _create_slot_and_resume(
    message: Message,
    state: FSMContext,
    *,
    data: dict,
    duration: int,
) -> None:
    try:
        business_id = str(data["business_id"])
        business_token = str(data["business_token"])
        offering_id = str(data["offering_id"])
        booking_start = str(data["booking_start"])
    except KeyError:
        await state.clear()
        await message.answer(
            "Этот шаг уже устарел. Нажмите «🚀 Получить клиентов» и я начну заново."
        )
        return

    try:
        actor = await control._actor(control._user_id(message), business_id)
        await asyncio.to_thread(
            control.create_booking_slot,
            actor=actor,
            offering_id=offering_id,
            local_start=booking_start,
            duration_minutes=duration,
        )
    except (BookingError, TenantPermissionDenied, ValueError, TypeError):
        await message.answer(
            "Такое время не получилось сохранить. Напишите дату и время ещё раз, "
            "например: 20.08.2026 12:00."
        )
        await state.set_state(GoalScheduleState.waiting_booking_start)
        return

    await message.answer(
        "✅ Время добавлено. Продолжаю готовить привлечение клиентов автоматически…"
    )
    await one_click.get_clients_one_click(
        _ResumeCallback(message=message, business_token=business_token),
        state,
    )


@router.callback_query(F.data.startswith("cpo:start:"))
async def get_clients_goal(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    slots = await asyncio.to_thread(control.list_booking_slots, actor=actor)
    if any(item.slot.status == BookingSlotStatus.OPEN for item in slots):
        await one_click.get_clients_one_click(callback, state)
        return

    await callback.answer("Готовлю всё сам…")
    await state.clear()
    await _begin_missing_schedule(
        callback,
        state,
        actor=actor,
        business_id=business_id,
        business_token=business_token,
    )


@router.callback_query(F.data.startswith("cpo:offers:"))
async def change_goal_offering_page(callback: CallbackQuery) -> None:
    try:
        _, _, business_token, raw_page = str(callback.data).split(":", 3)
        business_id = control._token_uuid(business_token)
        page = int(raw_page)
        if page < 0:
            raise ValueError
        actor = await control._actor(int(callback.from_user.id), business_id)
        offerings = await _selectable_offerings(actor)
    except (ValueError, TenantPermissionDenied):
        await callback.answer(
            "Список изменился. Нажмите «Получить клиентов» ещё раз.",
            show_alert=True,
        )
        return
    if not offerings:
        await callback.answer("Список услуг изменился. Начните ещё раз.", show_alert=True)
        return
    await callback.answer()
    await _show_offering_page(
        control._callback_message(callback),
        business_token=business_token,
        offerings=offerings,
        page=page,
    )


@router.callback_query(F.data.startswith("cpo:offer:"))
async def choose_goal_offering(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, business_token, offering_token = str(callback.data).split(":", 3)
        business_id = control._token_uuid(business_token)
        offering_id = control._token_uuid(offering_token)
        actor = await control._actor(int(callback.from_user.id), business_id)
        offering = await _find_offering(actor, offering_id)
    except (ValueError, TenantPermissionDenied):
        offering = None
    if offering is None:
        await callback.answer(
            "Не получилось выбрать услугу. Начните ещё раз.",
            show_alert=True,
        )
        return
    await callback.answer()
    await _ask_booking_start(
        control._callback_message(callback),
        state,
        business_id=business_id,
        business_token=business_token,
        offering=offering,
    )


@router.message(GoalScheduleState.waiting_offering_title)
async def receive_goal_offering_title(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    title = " ".join(str(message.text or "").split()).strip()
    if not title:
        await message.answer("Напишите короткое название, например: «Консультация».")
        return
    try:
        business_id = str(data["business_id"])
        business_token = str(data["business_token"])
        capability_id = str(data["capability_id"])
    except KeyError:
        await state.clear()
        await message.answer(
            "Этот шаг уже устарел. Нажмите «🚀 Получить клиентов» и я начну заново."
        )
        return
    try:
        actor = await control._actor(control._user_id(message), business_id)
        offering = await asyncio.to_thread(
            control.create_business_offering,
            actor=actor,
            capability_id=capability_id,
            title=title,
            description=f"{title}. Запись через ClientPlatform.",
        )
    except (ActivityError, TenantPermissionDenied, ValueError):
        await message.answer(
            "Не получилось сохранить название. Попробуйте написать его ещё раз."
        )
        return
    await _ask_booking_start(
        message,
        state,
        business_id=business_id,
        business_token=business_token,
        offering=offering,
    )


@router.message(GoalScheduleState.waiting_booking_start)
async def receive_goal_booking_start(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    value = " ".join(str(message.text or "").split()).strip()
    if not value:
        await message.answer("Напишите дату и время, например: 20.08.2026 12:00.")
        return
    next_data = {**data, "booking_start": value}
    await state.set_data(next_data)
    inferred = _duration_from_title(str(data.get("offering_title") or ""))
    if inferred is not None:
        await _create_slot_and_resume(
            message,
            state,
            data=next_data,
            duration=inferred,
        )
        return
    await state.set_state(GoalScheduleState.waiting_booking_duration)
    await message.answer(
        "Сколько минут обычно занимает эта встреча или услуга? Например: 60"
    )


@router.message(GoalScheduleState.waiting_booking_duration)
async def receive_goal_booking_duration(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        duration = int(str(message.text or "").strip())
    except ValueError:
        duration = 0
    if not 5 <= duration <= 720:
        await message.answer("Напишите только число минут, например: 60.")
        return
    await _create_slot_and_resume(message, state, data=data, duration=duration)


__all__ = ["GoalScheduleState", "get_clients_goal", "router"]

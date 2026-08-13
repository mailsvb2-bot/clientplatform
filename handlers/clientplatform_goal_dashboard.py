from __future__ import annotations

"""Information-rich goal-first owner dashboard presentation."""

import asyncio

from aiogram.types import Message

from clientplatform.domain.bookings import BookingSlotStatus

from . import clientplatform_control as control
from . import clientplatform_goal_first_safety as goal_contract
from . import clientplatform_one_click_experience as one_click
from . import clientplatform_owner_journey as owner


def _goal_keyboard(business_id: str):
    """Render the single canonical owner-home navigation.

    Acquisition and already-arrived customer conversations are deliberately
    separate actions.  Keeping both here prevents later presentation layers
    from hiding the sales workspace again.
    """

    token = control._uuid_token(business_id)
    action = goal_contract.ACQUIRE_CLIENTS
    return control._keyboard(
        [
            [(action.label, action.callback(token))],
            [("💬 Обращения и продажи", f"cps:s:{token}")],
            [
                ("👥 Клиенты и запись", f"cpj:bookings:{token}"),
                ("⚙️ Ещё", f"cpo:more:{token}"),
            ],
        ]
    )


async def send_goal_dashboard(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    actor, access, profile, capabilities, customers, programs, _snapshot_slots = (
        await one_click.simple._business_snapshot(
            user_id=user_id,
            business_id=business_id,
        )
    )
    offerings, slots = await asyncio.gather(
        owner._all_offerings(actor, capabilities),
        asyncio.to_thread(
            control.list_booking_slots,
            actor=actor,
            include_unavailable=True,
        ),
    )
    open_slots = [item for item in slots if item.slot.status == BookingSlotStatus.OPEN]
    booked_slots = [item for item in slots if item.slot.status == BookingSlotStatus.BOOKED]
    nearest = min(
        (
            item
            for item in slots
            if item.slot.status in {BookingSlotStatus.OPEN, BookingSlotStatus.BOOKED}
        ),
        key=lambda item: item.slot.starts_at,
        default=None,
    )
    nearest_line = (
        "Ближайшее время: пока не опубликовано"
        if nearest is None
        else f"Ближайшее время: {nearest.local_start} · {nearest.offering_title}"
    )
    readiness = (
        f"Свободных времён: {len(open_slots)}."
        if open_slots
        else "Свободных времён пока нет — если понадобится, я попрошу добавить одно."
    )
    action = goal_contract.ACQUIRE_CLIENTS
    await message.answer(
        f"🏠 {access.business.name}\n\n"
        f"{profile.activity_description}\n\n"
        f"Услуг: {len(offerings)} · свободных времён: {len(open_slots)} · "
        f"записей клиентов: {len(booked_slots)}\n"
        f"Материалов и программ: {len(programs)} · клиентов: {len(customers)}\n"
        f"{nearest_line}\n\n"
        f"{readiness}\n\n"
        "Что нужно сделать сейчас:\n"
        f"• «{action.label}» — подготовить продвижение и привести новых людей.\n"
        "• «💬 Обращения и продажи» — разобрать тех, кто уже написал: увидеть следующий "
        "шаг, подключить ИИ-помощника и подготовить ответ.\n\n"
        "Технические кабинеты и кампании знать не нужно. Действия с возможными "
        "расходами подтверждаются отдельно, а сообщения клиентам не отправляются без "
        "Вашего подтверждения.\n\n"
        "Остальные функции собраны в «⚙️ Ещё».",
        reply_markup=_goal_keyboard(business_id),
    )


__all__ = ["_goal_keyboard", "send_goal_dashboard"]

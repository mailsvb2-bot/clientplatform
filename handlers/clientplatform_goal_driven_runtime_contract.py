from __future__ import annotations

"""Runtime composition contract for the intent-first owner experience.

Keeps the familiar compact business status while making the primary journey
result-driven, and normalizes Telegram callback/message targets without leaking
provider implementation details into the user flow.
"""

import asyncio
from types import ModuleType

from aiogram.types import CallbackQuery

from clientplatform.domain.bookings import BookingSlotStatus


def _event_target(event, *, control_module: ModuleType):
    if isinstance(event, CallbackQuery):
        return control_module._callback_message(event)
    # Contract tests and managed bot gateways may use callback-shaped adapters
    # rather than aiogram's concrete class. Detect the callback shape, not an
    # arbitrary message implementation.
    if (
        getattr(event, "data", None) is not None
        and getattr(event, "message", None) is not None
        and not hasattr(event, "text")
    ):
        return control_module._callback_message(event)
    return event


def install_goal_runtime_contract(
    *,
    goal_module: ModuleType,
    owner_module: ModuleType,
    simple_module: ModuleType,
    control_module: ModuleType,
) -> None:
    if bool(getattr(goal_module, "_runtime_contract_installed", False)):
        return

    def target(event):
        return _event_target(event, control_module=control_module)

    async def send_dashboard(message, *, user_id: int, business_id: str) -> None:
        # Resolve the snapshot dependency through the goal module at call time.
        # Besides keeping tests patchable, this prevents lazy handler composition
        # from retaining a stale module reference if the simple experience is
        # extended/replaced later during startup.
        snapshot_module = getattr(goal_module, "simple", simple_module)
        actor, access, profile, capabilities, customers, programs, snapshot_slots = (
            await snapshot_module._business_snapshot(
                user_id=user_id,
                business_id=business_id,
            )
        )
        offerings = (
            await owner_module._all_offerings(actor, capabilities)
            if capabilities
            else []
        )
        slots = snapshot_slots
        # Owner status includes booked/unavailable entries as well as open ones.
        # The lightweight snapshot is enough when no scheduling capability exists;
        # otherwise read the canonical full calendar once for accurate status.
        if capabilities:
            slots = await asyncio.to_thread(
                control_module.list_booking_slots,
                actor=actor,
                include_unavailable=True,
            )
        open_slots = [
            item for item in slots if item.slot.status == BookingSlotStatus.OPEN
        ]
        booked_slots = [
            item for item in slots if item.slot.status == BookingSlotStatus.BOOKED
        ]
        activity = " ".join(
            str(getattr(profile, "activity_description", "") or "").split()
        )
        activity_block = f"\n{activity}\n" if activity else "\n"
        await message.answer(
            f"🏠 {access.business.name}\n"
            f"{activity_block}\n"
            f"Услуг: {len(offerings)} · свободных времён: {len(open_slots)} · "
            f"записей клиентов: {len(booked_slots)}\n"
            f"Материалов и программ: {len(programs)} · клиентов: {len(customers)}\n\n"
            "Что хотите получить?\n\n"
            "Если нужны новые клиенты — нажмите одну кнопку. Я сам выберу "
            "ближайшее свободное время, подготовлю текст, проверю доступное "
            "продвижение и использую уже известные настройки. Технические "
            "параметры выбирать не придётся.",
            reply_markup=goal_module._home_keyboard(business_id),
        )

    goal_module._target = target
    goal_module.send_goal_dashboard = send_dashboard
    owner_module.send_owner_dashboard = send_dashboard
    simple_module.send_simple_dashboard = send_dashboard
    control_module._send_dashboard = send_dashboard
    goal_module._runtime_contract_installed = True


__all__ = ["install_goal_runtime_contract"]

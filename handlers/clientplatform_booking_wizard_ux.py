from __future__ import annotations

import importlib
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, User

control = importlib.import_module(".clientplatform_control", __package__)
owner = importlib.import_module(".clientplatform_owner_journey", __package__)

router = Router(name="clientplatform_booking_wizard_ux")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

_QUICK_DURATIONS = (30, 45, 60, 90)


def _business_token(business_id: str) -> str:
    return control._uuid_token(business_id)


def _duration_keyboard(business_id: str):
    token = _business_token(business_id)
    return control._keyboard(
        [
            [
                ("30 мин", f"cpj:wizdur:{token}:30"),
                ("45 мин", f"cpj:wizdur:{token}:45"),
            ],
            [
                ("60 мин", f"cpj:wizdur:{token}:60"),
                ("90 мин", f"cpj:wizdur:{token}:90"),
            ],
            [("Другая длительность", f"cpj:wizcustom:{token}")],
            [("⬅️ Изменить дату и время", f"cpj:wizback:{token}")],
            [("✖️ Отмена", f"cpj:wizcancel:{token}")],
        ]
    )


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


@router.message(control.ClientPlatformControlState.booking_start)
async def receive_booking_start_with_quick_duration(
    message: Message,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    business_id = str(data.get("business_id") or "")
    if not business_id:
        await state.clear()
        await message.answer("Не удалось продолжить настройку. Откройте кабинет через /start.")
        return
    await state.update_data(booking_start=str(message.text or ""))
    await state.set_state(control.ClientPlatformControlState.booking_duration)
    prefix = "Новое время принято." if data.get("replacing_slot_id") else "Дата и время приняты."
    await message.answer(
        f"{prefix} Выберите длительность — обычно достаточно одного нажатия.\n\n"
        "Если нужного варианта нет, выберите «Другая длительность».",
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
    await owner.receive_owner_booking_duration(
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
    await callback.answer()
    await message.answer(
        "Напишите длительность встречи или услуги в минутах. Например: 75.",
        reply_markup=_cancel_keyboard(business_id),
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
    await state.set_state(control.ClientPlatformControlState.booking_start)
    message = control._callback_message(callback)
    await _remove_keyboard(message)
    await callback.answer()
    await message.answer(
        "Напишите дату и время заново: ДД.ММ.ГГГГ ЧЧ:ММ.\n"
        "Например: 15.08.2026 18:30.",
        reply_markup=control._keyboard(
            [[("✖️ Отмена", f"cpj:wizcancel:{_business_token(business_id)}")]]
        ),
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
    await owner.send_owner_dashboard(
        message,
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


__all__ = [
    "cancel_booking_wizard",
    "choose_custom_duration",
    "choose_quick_duration",
    "receive_booking_start_with_quick_duration",
    "return_to_booking_start",
    "router",
]

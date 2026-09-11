from __future__ import annotations

import asyncio
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from clientplatform.application.activity import get_business_profile
from clientplatform.application.event_owner_flow import (
    OnlineEventCreateRequest,
    create_and_publish_online_event,
)
from clientplatform.domain.bookings import parse_local_booking_start
from clientplatform.domain.tenancy import TenantPermissionDenied
from config.settings import settings

from . import clientplatform_control as control

router = Router(name="clientplatform_events")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())


class ClientPlatformEventState(StatesGroup):
    waiting_details = State()


def _cancel_keyboard(business_id: str):
    token = control._uuid_token(business_id)
    return control._keyboard([[("✖️ Отмена", f"cpev:cancel:{token}")]])


def _public_base_url() -> str:
    value = str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if not value.startswith("https://"):
        raise ValueError("публичный HTTPS-адрес ClientPlatform пока не настроен")
    return value


@router.callback_query(F.data.startswith("cpev:new:"))
async def start_event_wizard(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        actor.assert_can_manage_business()
    except TenantPermissionDenied:
        await callback.answer("Создавать мероприятия может владелец или администратор", show_alert=True)
        return
    await state.clear()
    await state.set_state(ClientPlatformEventState.waiting_details)
    await state.update_data(event_business_id=business_id)
    await callback.answer()
    await control._callback_message(callback).answer(
        "🎥 Новое онлайн-мероприятие\n\n"
        "Отправьте одной строкой:\n"
        "Название | ДД.ММ.ГГГГ ЧЧ:ММ | HTTPS-ссылка на эфир | ссылка предложения\n\n"
        "Последнее поле необязательно — вместо него можно поставить -.\n"
        "Площадка может быть любой: Zoom, Webinar.ru, МТС Линк, Телемост, VK, YouTube, RuTube или другой HTTPS-сервис.",
        reply_markup=_cancel_keyboard(business_id),
    )


@router.message(ClientPlatformEventState.waiting_details)
async def receive_event_details(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    if not business_id:
        await state.clear()
        await message.answer("Не удалось продолжить создание мероприятия. Откройте кабинет заново.")
        return
    actor = await control._actor(int(message.from_user.id), business_id)
    actor.assert_can_manage_business()
    parts = [part.strip() for part in str(message.text or "").split("|")]
    if len(parts) not in {3, 4} or not all(parts[:3]):
        await message.answer("Нужны 3–4 поля через |. Пример: Вебинар | 15.09.2026 19:00 | https://example.com/room | -", reply_markup=_cancel_keyboard(business_id))
        return
    title, local_time, join_url = parts[:3]
    offer_url = None if len(parts) == 3 or parts[3] in {"", "-"} else parts[3]
    try:
        profile = await asyncio.to_thread(get_business_profile, actor=actor)
        starts_at = datetime.fromisoformat(
            parse_local_booking_start(local_time, timezone_name=profile.timezone)
        )
        created = await asyncio.to_thread(
            create_and_publish_online_event,
            actor=actor,
            request=OnlineEventCreateRequest(
                title=title,
                starts_at=starts_at,
                timezone_name=profile.timezone,
                join_url=join_url,
                offer_url=offer_url,
            ),
        )
        registration_url = created.registration_url(_public_base_url())
    except (ValueError, RuntimeError) as exc:
        await message.answer(f"Не удалось создать мероприятие: {exc}\n\nИсправьте строку и отправьте её ещё раз.", reply_markup=_cancel_keyboard(business_id))
        return
    await state.clear()
    provider = created.provider_key if created.provider_key != "external" else "внешняя площадка"
    mail_note = "Напоминания по e-mail включены." if created.email_notifications_enabled else "E-mail не подключён — регистрация и ссылка входа всё равно работают."
    await message.answer(
        f"✅ Мероприятие опубликовано.\n\nПлощадка: {provider}\nРегистрация: {registration_url}\n\n{mail_note}",
        reply_markup=control._keyboard([[("🎥 Создать ещё", f"cpev:new:{control._uuid_token(business_id)}")], [("⬅️ К продвижению", f"cpo:content:{control._uuid_token(business_id)}")]]),
    )


@router.callback_query(F.data.startswith("cpev:cancel:"))
async def cancel_event_wizard(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(token)
    await control._actor(int(callback.from_user.id), business_id)
    data = await state.get_data()
    state_business = str(data.get("event_business_id") or "")
    if state_business and state_business != business_id:
        await callback.answer("Этот шаг уже устарел", show_alert=True)
        return
    await state.clear()
    await callback.answer("Создание отменено")
    await control._callback_message(callback).answer(
        "Создание мероприятия отменено.",
        reply_markup=control._keyboard([[('⬅️ К продвижению', f'cpo:content:{token}')]]),
    )


__all__ = ["ClientPlatformEventState", "cancel_event_wizard", "receive_event_details", "router", "start_event_wizard"]

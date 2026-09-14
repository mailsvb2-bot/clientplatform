from __future__ import annotations

import asyncio
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from clientplatform.application.activity import get_business_profile
from clientplatform.application.cockpit_events import resolve_cockpit_events
from clientplatform.application.event_owner_flow import (
    OnlineEventCreateRequest,
    create_and_publish_online_event,
)
from clientplatform.application.event_followup_settings import (
    get_business_event_followup_settings,
    set_business_event_followup_channel_enabled,
    set_business_event_followup_segment_enabled,
    set_business_event_followups_enabled,
)
from clientplatform.domain.bookings import parse_local_booking_start
from clientplatform.domain.tenancy import TenantPermissionDenied
from clientplatform.presentation.event_ui import (
    BACK_TO_EVENTS_LABEL,
    BACK_TO_GROWTH_LABEL,
    EVENT_CREATION_INPUT_GUIDANCE,
    event_creation_failure_text,
    event_creation_prompt,
    event_creation_success_text,
    event_settings_actions,
    event_settings_text,
)
from config.settings import settings

from . import clientplatform_control as control

router = Router(name="clientplatform_events")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())


class ClientPlatformEventState(StatesGroup):
    waiting_details = State()


def _cancel_keyboard(business_id: str):
    token = control._uuid_token(business_id)
    return control._keyboard([[(BACK_TO_EVENTS_LABEL, f"cpev:cancel:{token}")]])




def _settings_rows(snapshot: object, *, token: str) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    channel_row: list[tuple[str, str]] = []
    for action in event_settings_actions(snapshot):
        if action.kind == "followups":
            callback = f"cpev:followups:{'on' if action.enabled else 'off'}:{token}"
        elif action.kind == "segment" and action.key is not None:
            callback = f"cpev:seg:{action.key}:{'on' if action.enabled else 'off'}:{token}"
        elif action.kind == "channel" and action.key is not None:
            callback = f"cpev:ch:{action.key}:{'on' if action.enabled else 'off'}:{token}"
        else:
            raise ValueError("unsupported event settings action")
        button = (action.label, callback)
        if action.kind == "channel":
            channel_row.append(button)
        else:
            rows.append([button])
    if channel_row:
        rows.append(channel_row)
    rows.append([(BACK_TO_EVENTS_LABEL, f"cpev:home:{token}")])
    return rows


async def _send_event_settings(
    target, *, user_id: int, business_id: str
) -> None:
    snapshot = await asyncio.to_thread(
        resolve_cockpit_events,
        telegram_user_id=user_id,
        requested_business_id=business_id,
        limit=5,
    )
    token = control._uuid_token(business_id)
    await target.answer(
        event_settings_text(snapshot),
        reply_markup=control._keyboard(_settings_rows(snapshot, token=token)),
    )


def _public_base_url() -> str:
    value = str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if not value.startswith("https://"):
        raise ValueError("публичный HTTPS-адрес ClientPlatform пока не настроен")
    return value


@router.callback_query(F.data.startswith("cpev:home:"))
async def open_event_hub(callback: CallbackQuery) -> None:
    token = str(callback.data or "").split(":", 2)[2]
    business_id = control._token_uuid(token)
    await callback.answer()
    from .clientplatform_cockpit_dispatch import send_cockpit_section

    await send_cockpit_section(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        section="events",
    )


@router.callback_query(F.data.startswith("cpev:settings:"))
async def open_event_settings(callback: CallbackQuery) -> None:
    token = str(callback.data or "").split(":", 2)[2]
    business_id = control._token_uuid(token)
    await callback.answer()
    await _send_event_settings(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


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
    profile = await asyncio.to_thread(get_business_profile, actor=actor)
    await state.clear()
    await state.set_state(ClientPlatformEventState.waiting_details)
    await state.update_data(event_business_id=business_id)
    await callback.answer()
    await control._callback_message(callback).answer(
        event_creation_prompt(profile.timezone),
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
    if " ".join(str(message.text or "").strip().split()).casefold() in {"отмена", "cancel"}:
        await state.clear()
        await message.answer(
            "Создание вебинара отменено. Данные не изменены.",
            reply_markup=control._keyboard(
                [[(BACK_TO_EVENTS_LABEL, f"cpev:home:{control._uuid_token(business_id)}")]]
            ),
        )
        return
    parts = [part.strip() for part in str(message.text or "").split("|")]
    if len(parts) not in {3, 4} or not all(parts[:3]):
        await message.answer(
            "Не получилось понять ответ.\n\n"
            + EVENT_CREATION_INPUT_GUIDANCE
            + "\n\nЧтобы выйти без изменений, отправьте «Отмена» или нажмите «🎥 К вебинарам».",
            reply_markup=_cancel_keyboard(business_id),
        )
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
    except (ValueError, RuntimeError):
        await message.answer(
            event_creation_failure_text()
            + "\n\nИсправьте строку и отправьте её ещё раз.",
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    await state.clear()
    token = control._uuid_token(business_id)
    await message.answer(
        event_creation_success_text(
            title=title,
            local_time=local_time,
            provider_key=created.provider_key,
            registration_url=registration_url,
            email_notifications_enabled=created.email_notifications_enabled,
        ),
        reply_markup=control._keyboard(
            [
                [(BACK_TO_EVENTS_LABEL, f"cpev:home:{token}")],
                [("🎥 Создать ещё", f"cpev:new:{token}")],
                [(BACK_TO_GROWTH_LABEL, f"cpo:content:{token}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cpev:followups:"))
async def toggle_event_followups(callback: CallbackQuery) -> None:
    parts = str(callback.data or "").split(":", 3)
    if len(parts) != 4 or parts[2] not in {"on", "off"}:
        await callback.answer("Переключатель устарел", show_alert=True)
        return
    desired = parts[2] == "on"
    business_id = control._token_uuid(parts[3])
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        stored = await asyncio.to_thread(
            get_business_event_followup_settings, actor=actor
        )
        current = bool(stored and stored.enabled)
        if current != desired:
            await asyncio.to_thread(
                set_business_event_followups_enabled,
                actor=actor,
                enabled=desired,
            )
    except TenantPermissionDenied:
        await callback.answer(
            "Включить автоматические сообщения после мероприятия может владелец бизнеса",
            show_alert=True,
        )
        return
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await callback.answer("Автоматические сообщения включены" if desired else "Автоматические сообщения выключены")
    await _send_event_settings(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cpev:seg:"))
async def toggle_event_followup_segment(callback: CallbackQuery) -> None:
    parts = str(callback.data or "").split(":", 4)
    if len(parts) != 5 or parts[3] not in {"on", "off"}:
        await callback.answer("Настройка устарела", show_alert=True)
        return
    segment = parts[2]
    desired = parts[3] == "on"
    business_id = control._token_uuid(parts[4])
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        await asyncio.to_thread(
            set_business_event_followup_segment_enabled,
            actor=actor,
            segment=segment,
            enabled=desired,
        )
    except TenantPermissionDenied:
        await callback.answer(
            "Расширить группы участников вебинара может только владелец бизнеса",
            show_alert=True,
        )
        return
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Настройка участников сохранена")
    await _send_event_settings(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cpev:ch:"))
async def toggle_event_followup_channel(callback: CallbackQuery) -> None:
    parts = str(callback.data or "").split(":", 4)
    if len(parts) != 5 or parts[3] not in {"on", "off"}:
        await callback.answer("Настройка устарела", show_alert=True)
        return
    channel = parts[2]
    desired = parts[3] == "on"
    business_id = control._token_uuid(parts[4])
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        await asyncio.to_thread(
            set_business_event_followup_channel_enabled,
            actor=actor,
            channel=channel,
            enabled=desired,
        )
    except TenantPermissionDenied:
        await callback.answer(
            "Расширить каналы сообщений участникам вебинара может только владелец бизнеса",
            show_alert=True,
        )
        return
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Настройка канала сохранена")
    await _send_event_settings(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
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
        "Создание вебинара отменено. Данные не изменены.",
        reply_markup=control._keyboard([[(BACK_TO_EVENTS_LABEL, f"cpev:home:{token}")]]),
    )


__all__ = [
    "ClientPlatformEventState",
    "cancel_event_wizard",
    "open_event_hub",
    "open_event_settings",
    "receive_event_details",
    "router",
    "start_event_wizard",
    "toggle_event_followup_channel",
    "toggle_event_followup_segment",
    "toggle_event_followups",
]

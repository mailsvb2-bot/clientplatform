from __future__ import annotations

import asyncio
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from clientplatform.application.activity import get_business_profile
from clientplatform.application.event_owner_flow import (
    MultiSessionOnlineEventCreateRequest,
    OnlineEventCreateRequest,
    OnlineEventSessionCreateRequest,
    create_and_publish_multisession_online_event,
    create_and_publish_online_event,
)
from clientplatform.application.event_sessions import get_event_warmup_window
from clientplatform.application.event_warmups import get_event_warmup_plan
from clientplatform.domain.bookings import parse_local_booking_start
from clientplatform.domain.tenancy import TenantPermissionDenied
from clientplatform.presentation.event_ui import BACK_TO_EVENTS_LABEL
from config.settings import settings

from . import clientplatform_control as control


router = Router(name="clientplatform_event_lifecycle")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())


class ClientPlatformEventLifecycleState(StatesGroup):
    waiting_title = State()
    waiting_days = State()
    waiting_day1_time = State()
    waiting_day1_url = State()
    waiting_day2_time = State()
    waiting_day2_url = State()
    waiting_warmup_days = State()


def _cancel_keyboard(business_id: str):
    token = control._uuid_token(business_id)
    return control._keyboard([[(BACK_TO_EVENTS_LABEL, f"cpev:cancel:{token}")]])


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


def _parse_local_time(value: str, *, timezone_name: str) -> datetime:
    return datetime.fromisoformat(
        parse_local_booking_start(value, timezone_name=timezone_name)
    )


def _event_actions(
    *,
    event_id: str,
    business_id: str,
    join_ready: bool,
) -> list[list[tuple[str, str]]]:
    business_token = control._uuid_token(business_id)
    event_token = control._uuid_token(event_id)
    rows: list[list[tuple[str, str]]] = []
    if not join_ready:
        rows.append([("🔗 Добавить ссылку на эфир", f"cpev:join:{event_token}:{business_token}")])
    rows.append([("✨ Сделать анонс", f"cpev:announce:{event_token}:{business_token}")])
    rows.append([(BACK_TO_EVENTS_LABEL, f"cpev:home:{business_token}")])
    rows.append([("🎥 Создать ещё", f"cpev:new:{business_token}")])
    return rows


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
        f"Сколько дней прогревать аудиторию? Введите число от 0 до {window.max_warmup_days}.\n"
        "0 — пропустить прогрев. ClientPlatform подготовит тексты как черновики владельца и ничего не разошлёт без разрешённого канала.",
        reply_markup=_cancel_keyboard(business_id),
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
        "Сколько дней идёт мероприятие?\n\nВведите 1 или 2.",
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
    raw = _normalized_text(message).casefold()
    if raw in {"1", "1 день", "один", "один день"}:
        days = 1
    elif raw in {"2", "2 дня", "два", "два дня"}:
        days = 2
    else:
        await message.answer("Введите 1 или 2.", reply_markup=_cancel_keyboard(business_id))
        return
    actor = await control._actor(int(message.from_user.id), business_id)
    profile = await asyncio.to_thread(get_business_profile, actor=actor)
    await state.update_data(event_days=days, event_timezone=profile.timezone)
    await state.set_state(ClientPlatformEventLifecycleState.waiting_day1_time)
    await message.answer(
        "Когда начинается день 1?\n\n"
        "Напишите дату и время, например: 25.09.2026 19:00\n"
        f"Часовой пояс бизнеса: {profile.timezone}.",
        reply_markup=_cancel_keyboard(business_id),
    )


@router.message(ClientPlatformEventLifecycleState.waiting_day1_time)
async def receive_day1_time(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    timezone_name = str(data.get("event_timezone") or "")
    if not business_id or not timezone_name:
        await state.clear()
        await message.answer("Не удалось продолжить. Откройте вебинары заново.")
        return
    if _is_cancel(message):
        await _cancel(message, state, business_id)
        return
    local_time = _normalized_text(message)
    try:
        starts_at = _parse_local_time(local_time, timezone_name=timezone_name)
    except ValueError:
        await message.answer(
            "Не удалось понять дату и время. Напишите, например: 25.09.2026 19:00",
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    await state.update_data(day1_local_time=local_time, day1_starts_at=starts_at.isoformat())
    await state.set_state(ClientPlatformEventLifecycleState.waiting_day1_url)
    days = int(data.get("event_days") or 1)
    optional = " Можно отправить «-», если ссылка появится позже." if days == 1 else ""
    await message.answer(
        "Пришлите HTTPS-ссылку на комнату дня 1." + optional,
        reply_markup=_cancel_keyboard(business_id),
    )


@router.message(ClientPlatformEventLifecycleState.waiting_day1_url)
async def receive_day1_url(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    if not business_id:
        await state.clear()
        await message.answer("Не удалось продолжить. Откройте вебинары заново.")
        return
    if _is_cancel(message):
        await _cancel(message, state, business_id)
        return
    days = int(data.get("event_days") or 1)
    raw_url = str(message.text or "").strip()
    day1_url = None if raw_url == "-" else raw_url
    if days == 2 and not day1_url:
        await message.answer(
            "Для двухдневного вебинара нужна отдельная HTTPS-ссылка дня 1.",
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    if day1_url and not day1_url.startswith("https://"):
        await message.answer("Ссылка должна начинаться с https://", reply_markup=_cancel_keyboard(business_id))
        return
    await state.update_data(day1_url=day1_url)
    if days == 2:
        await state.set_state(ClientPlatformEventLifecycleState.waiting_day2_time)
        await message.answer(
            "Когда начинается день 2?\n\nНапишите дату и время второго дня.",
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    await _create_single_day_event(message, state)


async def _create_single_day_event(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data["event_business_id"])
    actor = await control._actor(int(message.from_user.id), business_id)
    try:
        created = await asyncio.to_thread(
            create_and_publish_online_event,
            actor=actor,
            request=OnlineEventCreateRequest(
                title=str(data["event_title"]),
                starts_at=datetime.fromisoformat(str(data["day1_starts_at"])),
                timezone_name=str(data["event_timezone"]),
                join_url=data.get("day1_url"),
            ),
        )
        registration_url = created.registration_url(_public_base_url())
        await _begin_warmup_choice(
            message,
            state,
            actor=actor,
            business_id=business_id,
            event_id=created.event_id,
            title=str(data["event_title"]),
            local_times=(str(data["day1_local_time"]),),
            registration_url=registration_url,
            provider_key=created.provider_key,
            join_ready=created.join_ready,
        )
    except (ValueError, RuntimeError):
        await message.answer(
            "Не удалось создать вебинар. Проверьте дату и HTTPS-ссылку.",
            reply_markup=_cancel_keyboard(business_id),
        )


@router.message(ClientPlatformEventLifecycleState.waiting_day2_time)
async def receive_day2_time(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    timezone_name = str(data.get("event_timezone") or "")
    if not business_id or not timezone_name:
        await state.clear()
        await message.answer("Не удалось продолжить. Откройте вебинары заново.")
        return
    if _is_cancel(message):
        await _cancel(message, state, business_id)
        return
    local_time = _normalized_text(message)
    try:
        starts_at = _parse_local_time(local_time, timezone_name=timezone_name)
        day1 = datetime.fromisoformat(str(data["day1_starts_at"]))
        if starts_at <= day1:
            raise ValueError("day 2 must start after day 1")
    except (KeyError, ValueError):
        await message.answer(
            "День 2 должен начинаться позже дня 1. Проверьте дату и время.",
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    await state.update_data(day2_local_time=local_time, day2_starts_at=starts_at.isoformat())
    await state.set_state(ClientPlatformEventLifecycleState.waiting_day2_url)
    await message.answer(
        "Пришлите HTTPS-ссылку на комнату дня 2. Она должна отличаться от ссылки дня 1.",
        reply_markup=_cancel_keyboard(business_id),
    )


@router.message(ClientPlatformEventLifecycleState.waiting_day2_url)
async def receive_day2_url(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    if not business_id:
        await state.clear()
        await message.answer("Не удалось продолжить. Откройте вебинары заново.")
        return
    if _is_cancel(message):
        await _cancel(message, state, business_id)
        return
    day2_url = str(message.text or "").strip()
    day1_url = str(data.get("day1_url") or "").strip()
    if not day2_url.startswith("https://"):
        await message.answer("Ссылка должна начинаться с https://", reply_markup=_cancel_keyboard(business_id))
        return
    if day2_url == day1_url:
        await message.answer(
            "Для дня 2 нужна другая ссылка на комнату.",
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    actor = await control._actor(int(message.from_user.id), business_id)
    try:
        created = await asyncio.to_thread(
            create_and_publish_multisession_online_event,
            actor=actor,
            request=MultiSessionOnlineEventCreateRequest(
                title=str(data["event_title"]),
                timezone_name=str(data["event_timezone"]),
                sessions=(
                    OnlineEventSessionCreateRequest(
                        starts_at=datetime.fromisoformat(str(data["day1_starts_at"])),
                        join_url=day1_url,
                    ),
                    OnlineEventSessionCreateRequest(
                        starts_at=datetime.fromisoformat(str(data["day2_starts_at"])),
                        join_url=day2_url,
                    ),
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
            title=str(data["event_title"]),
            local_times=(str(data["day1_local_time"]), str(data["day2_local_time"])),
            registration_url=registration_url,
            provider_key=created.provider_key,
            join_ready=created.join_ready,
        )
    except (KeyError, ValueError, RuntimeError):
        await message.answer(
            "Не удалось создать двухдневный вебинар. Проверьте даты и обе HTTPS-ссылки.",
            reply_markup=_cancel_keyboard(business_id),
        )


@router.message(ClientPlatformEventLifecycleState.waiting_warmup_days)
async def receive_warmup_days(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    event_id = str(data.get("created_event_id") or "")
    if not business_id or not event_id:
        await state.clear()
        await message.answer("Вебинар создан, но не удалось продолжить прогрев. Откройте вебинары заново.")
        return
    if _is_cancel(message):
        await state.clear()
        await message.answer(
            "Вебинар уже создан. Настройка прогрева остановлена.",
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
            get_event_warmup_plan,
            actor=actor,
            event_id=event_id,
            requested_days=requested_days,
        )
    except (ValueError, RuntimeError):
        await message.answer(
            "Не удалось подготовить прогрев. Попробуйте другое число дней.",
            reply_markup=_cancel_keyboard(business_id),
        )
        return

    title = str(data.get("created_title") or "Вебинар")
    local_times = tuple(str(item) for item in data.get("created_local_times", []))
    registration_url = str(data.get("created_registration_url") or "")
    join_ready = bool(data.get("created_join_ready"))
    await state.clear()
    schedule = "\n".join(
        f"День {index}: {value}" for index, value in enumerate(local_times, start=1)
    )
    await message.answer(
        f"✅ {title}\n{schedule}\n\nРегистрация: {registration_url}\n\n"
        f"Прогрев: {plan.requested_days} дн. Напоминания участникам будут привязаны к каждому дню и его собственной комнате.",
        reply_markup=control._keyboard(
            _event_actions(
                event_id=event_id,
                business_id=business_id,
                join_ready=join_ready,
            )
        ),
    )
    for draft in plan.drafts:
        await message.answer(
            f"🔥 Прогрев {draft.position}/{plan.requested_days} — {draft.publish_date.strftime('%d.%m.%Y')}\n\n{draft.text}"
        )


__all__ = ["ClientPlatformEventLifecycleState", "router"]

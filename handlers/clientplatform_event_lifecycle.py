from __future__ import annotations

import asyncio
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from clientplatform.application.event_owner_flow import (
    MultiSessionOnlineEventCreateRequest,
    OnlineEventCreateRequest,
    OnlineEventSessionCreateRequest,
    create_and_publish_multisession_online_event,
    create_and_publish_online_event,
)
from clientplatform.application.event_sessions import get_event_warmup_window
from clientplatform.application.event_warmups import get_event_warmup_plan
from clientplatform.application.event_wizard import (
    MAX_EVENT_SESSIONS,
    MOSCOW_TIMEZONE,
    EventWizardSession,
    normalize_event_timezone,
    normalize_session_join_url,
    parse_session_count,
    parse_session_window,
    validate_session_sequence,
)
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
    waiting_timezone = State()
    waiting_session_time = State()
    waiting_session_url = State()
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


def _session_from_payload(value: object) -> EventWizardSession:
    if not isinstance(value, dict):
        raise ValueError("invalid session payload")
    return EventWizardSession(
        position=int(value["position"]),
        starts_at=datetime.fromisoformat(str(value["starts_at"])),
        ends_at=datetime.fromisoformat(str(value["ends_at"])),
        local_label=str(value["local_label"]),
        join_url=(None if not str(value.get("join_url") or "").strip() else str(value["join_url"])),
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
    await state.update_data(event_days=days, event_sessions=[], event_session_index=1)
    await state.set_state(ClientPlatformEventLifecycleState.waiting_timezone)
    await message.answer(
        "По какому времени идут эфиры?\n\n"
        "Напишите «Москва» для московского времени. Если время другое — укажите часовой пояс, например Europe/Amsterdam или Asia/Yekaterinburg.",
        reply_markup=_cancel_keyboard(business_id),
    )


@router.message(ClientPlatformEventLifecycleState.waiting_timezone)
async def receive_timezone(message: Message, state: FSMContext) -> None:
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
    try:
        timezone_name = normalize_event_timezone(_normalized_text(message))
    except ValueError:
        await message.answer(
            "Не удалось определить часовой пояс. Напишите «Москва» или IANA-зону, например Europe/Amsterdam.",
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    await state.update_data(event_timezone=timezone_name, event_session_index=1)
    await _prompt_session_time(
        message,
        state,
        business_id=business_id,
        position=1,
        total=days,
        timezone_name=timezone_name,
    )


@router.message(ClientPlatformEventLifecycleState.waiting_session_time)
async def receive_session_time(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    timezone_name = str(data.get("event_timezone") or "")
    total = int(data.get("event_days") or 0)
    position = int(data.get("event_session_index") or 0)
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
    await state.update_data(
        pending_session={
            "position": session.position,
            "starts_at": session.starts_at.isoformat(),
            "ends_at": session.ends_at.isoformat(),
            "local_label": session.local_label,
        }
    )
    await state.set_state(ClientPlatformEventLifecycleState.waiting_session_url)
    await message.answer(
        f"Пришлите HTTPS-ссылку на комнату дня {position}.\n"
        "Если ссылка появится позже — отправьте «-»; добавить её можно будет до эфира.",
        reply_markup=_cancel_keyboard(business_id),
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
                ),
            )
        else:
            created = await asyncio.to_thread(
                create_and_publish_multisession_online_event,
                actor=actor,
                request=MultiSessionOnlineEventCreateRequest(
                    title=title,
                    timezone_name=timezone_name,
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
            local_times=tuple(session.local_label for session in sessions),
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
    except (KeyError, TypeError, ValueError) as exc:
        text = str(exc)
        if "own room" in text:
            answer = "Для каждого дня нужна своя ссылка на комнату. Эта ссылка уже используется другим днём."
        else:
            answer = "Ссылка должна начинаться с https://. Если её пока нет — отправьте «-»."
        await message.answer(answer, reply_markup=_cancel_keyboard(business_id))
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
        await _prompt_session_time(
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

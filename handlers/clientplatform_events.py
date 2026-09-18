from __future__ import annotations

import asyncio
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.activity import get_business_profile
from clientplatform.application.cockpit_events import resolve_cockpit_events
from clientplatform.application.event_announcements import draft_event_announcement
from clientplatform.application.event_content_plans import get_event_content_plan
from clientplatform.application.event_followups import event_followup_template_previews
from clientplatform.application.event_sessions import get_event_warmup_window
from clientplatform.application.event_warmups import (
    get_saved_event_warmup_plan,
    reset_event_warmup_text,
    save_event_warmup_plan,
    set_event_warmup_text,
)
from clientplatform.application.events import set_event_join_target
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
from clientplatform.domain.event_content import event_content_mode_label
from clientplatform.domain.tenancy import TenantPermissionDenied
from clientplatform.presentation.event_ui import (
    BACK_TO_EVENTS_LABEL,
    BACK_TO_GROWTH_LABEL,
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
    waiting_time = State()
    waiting_join_url = State()
    waiting_warmup_days = State()
    waiting_content_text = State()


def _cancel_keyboard(business_id: str):
    token = control._uuid_token(business_id)
    return control._keyboard([[(BACK_TO_EVENTS_LABEL, f"cpev:cancel:{token}")]])



def _announcement_share_markup(
    *,
    text: str,
    title: str,
    telegram_url: str,
    vk_url: str,
    max_url: str,
    business_token: str,
) -> InlineKeyboardMarkup:
    telegram_share = (
        "https://t.me/share/url?url="
        + quote(telegram_url, safe="")
        + "&text="
        + quote(text, safe="")
    )
    vk_share = (
        "https://vk.com/share.php?url="
        + quote(vk_url, safe="")
        + "&title="
        + quote(title, safe="")
        + "&comment="
        + quote(text, safe="")
    )
    max_share = "https://max.ru/:share?text=" + quote(
        f"{text}\n\nРегистрация: {max_url}", safe=""
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✈️ Опубликовать в Telegram", url=telegram_share)],
            [InlineKeyboardButton(text="🔵 Опубликовать во ВКонтакте", url=vk_share)],
            [InlineKeyboardButton(text="🟣 Опубликовать в MAX", url=max_share)],
            [
                InlineKeyboardButton(
                    text="📣 Запустить рекламу",
                    callback_data=f"cpj:promote:{business_token}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=BACK_TO_EVENTS_LABEL,
                    callback_data=f"cpev:home:{business_token}",
                )
            ],
        ]
    )


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


def _event_item(snapshot: object, event_id: str):
    for item in tuple(getattr(snapshot, "items", ())):
        if str(getattr(item, "id", "")) == event_id:
            return item
    raise ValueError("вебинар не найден")


def _content_plan_rows(
    *,
    event_id: str,
    business_id: str,
    has_warmup: bool,
) -> list[list[tuple[str, str]]]:
    event_token = control._uuid_token(event_id)
    business_token = control._uuid_token(business_id)
    rows: list[list[tuple[str, str]]] = []
    if has_warmup:
        rows.append([("🔥 Тексты прогрева", f"cpev:warmtxt:{event_token}:1:{business_token}")])
        rows.append([("🗓 Изменить дни прогрева", f"cpev:warmsetup:{event_token}:{business_token}")])
    else:
        rows.append([("🔥 Настроить прогрев", f"cpev:warmsetup:{event_token}:{business_token}")])
    rows.extend(
        [
            [("✨ Анонс", f"cpev:announce:{event_token}:{business_token}")],
            [("💬 Тексты дожима", f"cpev:followplan:{event_token}:{business_token}")],
            [("⚙️ Автосообщения", f"cpev:settings:{business_token}")],
            [(BACK_TO_EVENTS_LABEL, f"cpev:home:{business_token}")],
        ]
    )
    return rows


async def _send_event_content_plan(
    target,
    *,
    user_id: int,
    business_id: str,
    event_id: str,
) -> None:
    actor = await control._actor(user_id, business_id)
    actor.assert_can_manage_business()
    snapshot = await asyncio.to_thread(
        resolve_cockpit_events,
        telegram_user_id=user_id,
        requested_business_id=business_id,
        limit=30,
    )
    item = _event_item(snapshot, event_id)
    warmup = await asyncio.to_thread(
        get_saved_event_warmup_plan,
        actor=actor,
        event_id=event_id,
    )
    modes = await asyncio.to_thread(
        get_event_content_plan,
        actor=actor,
        event_id=event_id,
    )
    enabled = bool(getattr(snapshot, "commercial_followups_enabled", False))
    effective = bool(getattr(snapshot, "commercial_followups_effective", False))
    if enabled and effective:
        autosend = "🟢 включены"
    elif enabled:
        autosend = "🟡 включены, но сейчас ограничены политикой/платформой"
    else:
        autosend = "⚪️ выключены"

    if warmup.drafts:
        first = warmup.drafts[0].publish_date.strftime("%d.%m")
        last = warmup.drafts[-1].publish_date.strftime("%d.%m")
        warmup_line = (
            f"{warmup.requested_days} дн. · по 1 сообщению в день · "
            f"12:00 ({warmup.timezone_name}) · {first}–{last}"
        )
    else:
        warmup_line = "не настроен"

    text = (
        f"🗓 Контент-план\n\n{item.title}\n\n"
        f"🔥 Прогрев: {warmup_line}\n"
        f"   Формат: {event_content_mode_label(modes.warmup)}\n\n"
        "✨ Анонс: создаётся по кнопке и показывается Вам до публикации.\n"
        f"   Формат: {event_content_mode_label(modes.event_day)}\n\n"
        "🔔 Организационные сообщения: подтверждение регистрации, затем "
        "за 24 часа, 3 часа и 15 минут до каждого эфира.\n\n"
        "💬 Дожим: 2 сообщения для не пришедших/неподтверждённых и "
        "3 сообщения для участников/открывших предложение.\n"
        f"   Формат: {event_content_mode_label(modes.post_event)}\n\n"
        f"Автоматическая отправка: {autosend}.\n"
        "Прогрев отправляется только участникам с действующим согласием "
        "на коммерческие сообщения и только по разрешённому каналу."
    )
    await target.answer(
        text,
        reply_markup=control._keyboard(
            _content_plan_rows(
                event_id=event_id,
                business_id=business_id,
                has_warmup=bool(warmup.drafts),
            )
        ),
    )


async def _send_warmup_preview(
    target,
    *,
    user_id: int,
    business_id: str,
    event_id: str,
    position: int,
) -> None:
    actor = await control._actor(user_id, business_id)
    actor.assert_can_manage_business()
    plan = await asyncio.to_thread(
        get_saved_event_warmup_plan,
        actor=actor,
        event_id=event_id,
    )
    if not plan.drafts:
        await _send_event_content_plan(
            target,
            user_id=user_id,
            business_id=business_id,
            event_id=event_id,
        )
        return
    index = max(0, min(position - 1, len(plan.drafts) - 1))
    draft = plan.drafts[index]
    event_token = control._uuid_token(event_id)
    business_token = control._uuid_token(business_id)
    rows: list[list[tuple[str, str]]] = []
    nav: list[tuple[str, str]] = []
    if index > 0:
        nav.append(("⬅️", f"cpev:warmtxt:{event_token}:{index}:{business_token}"))
    if index + 1 < len(plan.drafts):
        nav.append(("➡️", f"cpev:warmtxt:{event_token}:{index + 2}:{business_token}"))
    if nav:
        rows.append(nav)
    rows.append(
        [("✏️ Изменить / написать свой", f"cpev:warmedit:{event_token}:{draft.position}:{business_token}")]
    )
    if draft.source == "owner":
        rows.append(
            [("♻️ Вернуть автотекст", f"cpev:warmreset:{event_token}:{draft.position}:{business_token}")]
        )
    rows.append([("🗓 К контент-плану", f"cpev:content:{event_token}:{business_token}")])
    local_at = draft.scheduled_at.astimezone(ZoneInfo(plan.timezone_name))
    source_label = "Ваш текст" if draft.source == "owner" else "Автотекст"
    await target.answer(
        f"🔥 Прогрев {draft.position}/{plan.requested_days}\n"
        f"Отправка: {local_at.strftime('%d.%m.%Y %H:%M')} ({plan.timezone_name})\n"
        f"Источник: {source_label}\n\n"
        f"{draft.text}\n\n"
        "Можно использовать {name}, {title}, {join_url}. "
        "Если {join_url} не указан, персональная ссылка на эфир добавится автоматически.",
        reply_markup=control._keyboard(rows),
    )


async def _send_followup_plan(
    target,
    *,
    user_id: int,
    business_id: str,
    event_id: str,
) -> None:
    snapshot = await asyncio.to_thread(
        resolve_cockpit_events,
        telegram_user_id=user_id,
        requested_business_id=business_id,
        limit=30,
    )
    item = _event_item(snapshot, event_id)
    previews = event_followup_template_previews()
    lines = [
        f"💬 Дожим после «{item.title}»",
        "",
        "Это тексты того же канонического движка, который ставит сообщения в отправку.",
    ]
    current_segment = None
    for preview in previews:
        if preview.segment != current_segment:
            current_segment = preview.segment
            lines.extend(["", f"• {preview.segment_label}:"])
        body = (
            preview.text.replace("{name}", "Имя")
            .replace("{title}", item.title)
            .replace("{offer}", "[ссылка на предложение]")
        )
        lines.append(f"  {preview.offset_label}: {body}")
    lines.extend(
        [
            "",
            "После оплаты дальнейший коммерческий дожим прекращается. "
            "Без действующего согласия сообщение не отправляется.",
        ]
    )
    token = control._uuid_token(business_id)
    event_token = control._uuid_token(event_id)
    await target.answer(
        "\n".join(lines),
        reply_markup=control._keyboard(
            [
                [("⚙️ Настроить автосообщения", f"cpev:settings:{token}")],
                [("🗓 К контент-плану", f"cpev:content:{event_token}:{token}")],
            ]
        ),
    )


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


@router.callback_query(F.data.startswith("cpev:content:"))
async def open_event_content_plan(callback: CallbackQuery) -> None:
    parts = str(callback.data or "").split(":", 3)
    if len(parts) != 4:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    event_id = control._token_uuid(parts[2])
    business_id = control._token_uuid(parts[3])
    await callback.answer()
    await _send_event_content_plan(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        event_id=event_id,
    )


@router.callback_query(F.data.startswith("cpev:warmsetup:"))
async def open_warmup_setup(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data or "").split(":", 3)
    if len(parts) != 4:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    event_id = control._token_uuid(parts[2])
    business_id = control._token_uuid(parts[3])
    actor = await control._actor(int(callback.from_user.id), business_id)
    actor.assert_can_manage_business()
    window = await asyncio.to_thread(
        get_event_warmup_window,
        actor=actor,
        event_id=event_id,
    )
    maximum = int(window.max_warmup_days)
    values = [value for value in (0, 1, 2, 3, 5, 7, 10, 14) if value <= maximum]
    if maximum not in values:
        values.append(maximum)
    values = sorted(set(values))
    event_token = control._uuid_token(event_id)
    business_token = control._uuid_token(business_id)
    rows = [
        [
            (str(value), f"cpev:warmdays:{event_token}:{value}:{business_token}")
            for value in values[index : index + 3]
        ]
        for index in range(0, len(values), 3)
    ]
    if maximum > 0:
        rows.append([("✍️ Другое число", f"cpev:warmother:{event_token}:{business_token}")])
    rows.append([("🗓 К контент-плану", f"cpev:content:{event_token}:{business_token}")])
    await state.clear()
    await callback.answer()
    await control._callback_message(callback).answer(
        f"🔥 Сколько дней прогрева сделать?\n\n"
        f"До первого эфира можно поставить до {maximum} дн. "
        "Будет ровно одно сообщение в день в 12:00 по часовому поясу вебинара. "
        "0 — отключить прогрев.",
        reply_markup=control._keyboard(rows),
    )


@router.callback_query(F.data.startswith("cpev:warmdays:"))
async def set_warmup_days(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data or "").split(":", 4)
    if len(parts) != 5 or not parts[3].isdigit():
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    event_id = control._token_uuid(parts[2])
    requested_days = int(parts[3])
    business_id = control._token_uuid(parts[4])
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        await asyncio.to_thread(
            save_event_warmup_plan,
            actor=actor,
            event_id=event_id,
            requested_days=requested_days,
        )
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer("Прогрев сохранён")
    await _send_event_content_plan(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        event_id=event_id,
    )


@router.callback_query(F.data.startswith("cpev:warmother:"))
async def request_custom_warmup_days(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data or "").split(":", 3)
    if len(parts) != 4:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    event_id = control._token_uuid(parts[2])
    business_id = control._token_uuid(parts[3])
    actor = await control._actor(int(callback.from_user.id), business_id)
    window = await asyncio.to_thread(
        get_event_warmup_window,
        actor=actor,
        event_id=event_id,
    )
    await state.clear()
    await state.set_state(ClientPlatformEventState.waiting_warmup_days)
    await state.update_data(
        event_business_id=business_id,
        content_event_id=event_id,
        max_warmup_days=int(window.max_warmup_days),
    )
    await callback.answer()
    await control._callback_message(callback).answer(
        f"Отправьте число дней от 0 до {int(window.max_warmup_days)}. "
        "0 отключит прогрев."
    )


@router.message(ClientPlatformEventState.waiting_warmup_days)
async def receive_custom_warmup_days(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    event_id = str(data.get("content_event_id") or "")
    maximum = int(data.get("max_warmup_days") or 0)
    raw = " ".join(str(message.text or "").split())
    if raw.casefold() in {"отмена", "cancel"}:
        await state.clear()
        if business_id and event_id:
            await _send_event_content_plan(
                message,
                user_id=int(message.from_user.id),
                business_id=business_id,
                event_id=event_id,
            )
        return
    if not raw.isdigit() or not 0 <= int(raw) <= maximum:
        await message.answer(f"Нужно число от 0 до {maximum}.")
        return
    actor = await control._actor(int(message.from_user.id), business_id)
    await asyncio.to_thread(
        save_event_warmup_plan,
        actor=actor,
        event_id=event_id,
        requested_days=int(raw),
    )
    await state.clear()
    await _send_event_content_plan(
        message,
        user_id=int(message.from_user.id),
        business_id=business_id,
        event_id=event_id,
    )


@router.callback_query(F.data.startswith("cpev:warmtxt:"))
async def open_warmup_text(callback: CallbackQuery) -> None:
    parts = str(callback.data or "").split(":", 4)
    if len(parts) != 5 or not parts[3].isdigit():
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    event_id = control._token_uuid(parts[2])
    business_id = control._token_uuid(parts[4])
    await callback.answer()
    await _send_warmup_preview(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        event_id=event_id,
        position=int(parts[3]),
    )


@router.callback_query(F.data.startswith("cpev:warmedit:"))
async def edit_warmup_text(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data or "").split(":", 4)
    if len(parts) != 5 or not parts[3].isdigit():
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    event_id = control._token_uuid(parts[2])
    position = int(parts[3])
    business_id = control._token_uuid(parts[4])
    actor = await control._actor(int(callback.from_user.id), business_id)
    actor.assert_can_manage_business()
    await state.clear()
    await state.set_state(ClientPlatformEventState.waiting_content_text)
    await state.update_data(
        event_business_id=business_id,
        content_event_id=event_id,
        content_position=position,
    )
    await callback.answer()
    await control._callback_message(callback).answer(
        f"✏️ Пришлите новый текст прогрева {position} одним сообщением.\n\n"
        "Можно написать текст полностью самостоятельно. Поддерживаются "
        "{name}, {title}, {join_url}. Персональная ссылка добавится автоматически, "
        "если {join_url} не вставлен.\n\nДля выхода: Отмена."
    )


@router.message(ClientPlatformEventState.waiting_content_text)
async def receive_warmup_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    event_id = str(data.get("content_event_id") or "")
    position = int(data.get("content_position") or 0)
    body = str(message.text or "").strip()
    if body.casefold() in {"отмена", "cancel"}:
        await state.clear()
        await _send_warmup_preview(
            message,
            user_id=int(message.from_user.id),
            business_id=business_id,
            event_id=event_id,
            position=position,
        )
        return
    if not 1 <= len(body) <= 3500:
        await message.answer("Текст должен быть от 1 до 3500 символов.")
        return
    actor = await control._actor(int(message.from_user.id), business_id)
    try:
        await asyncio.to_thread(
            set_event_warmup_text,
            actor=actor,
            event_id=event_id,
            position=position,
            text=body,
        )
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await _send_warmup_preview(
        message,
        user_id=int(message.from_user.id),
        business_id=business_id,
        event_id=event_id,
        position=position,
    )


@router.callback_query(F.data.startswith("cpev:warmreset:"))
async def reset_warmup_text(callback: CallbackQuery) -> None:
    parts = str(callback.data or "").split(":", 4)
    if len(parts) != 5 or not parts[3].isdigit():
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    event_id = control._token_uuid(parts[2])
    position = int(parts[3])
    business_id = control._token_uuid(parts[4])
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        await asyncio.to_thread(
            reset_event_warmup_text,
            actor=actor,
            event_id=event_id,
            position=position,
        )
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Автотекст восстановлен")
    await _send_warmup_preview(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        event_id=event_id,
        position=position,
    )


@router.callback_query(F.data.startswith("cpev:followplan:"))
async def open_followup_content_plan(callback: CallbackQuery) -> None:
    parts = str(callback.data or "").split(":", 3)
    if len(parts) != 4:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    event_id = control._token_uuid(parts[2])
    business_id = control._token_uuid(parts[3])
    await callback.answer()
    await _send_followup_plan(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        event_id=event_id,
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
    raw = str(message.text or "").strip()
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) in {2, 3, 4} and all(parts[:2]):
        # Keep the old one-line form as an expert shortcut, but do not require it.
        title, local_time = parts[:2]
        join_url = None if len(parts) < 3 or parts[2] in {"", "-"} else parts[2]
        offer_url = None if len(parts) < 4 or parts[3] in {"", "-"} else parts[3]
    else:
        title = raw
        if not title:
            await message.answer(
                "Введите название вебинара.",
                reply_markup=_cancel_keyboard(business_id),
            )
            return
        await state.update_data(event_title=title)
        await state.set_state(ClientPlatformEventState.waiting_time)
        profile = await asyncio.to_thread(get_business_profile, actor=actor)
        await message.answer(
            "Когда провести вебинар?\n\n"
            "Напишите дату и время, например: 16.09.2026 23:30\n"
            f"Часовой пояс бизнеса: {profile.timezone}.\n\n"
            "Ссылку на Zoom, Webinar.ru или другую площадку сейчас вводить не нужно — "
            "после создания появится отдельная кнопка «🔗 Добавить ссылку на эфир».",
            reply_markup=_cancel_keyboard(business_id),
        )
        return
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
    join_ready = bool(getattr(created, "join_ready", True))
    created_event_id = str(getattr(created, "event_id", "") or "").strip()
    event_rows: list[list[tuple[str, str]]] = []
    if created_event_id and not join_ready:
        event_rows.append(
            [(
                "🔗 Добавить ссылку на эфир",
                f"cpev:join:{control._uuid_token(created_event_id)}:{token}",
            )]
        )
    if created_event_id:
        event_rows.append(
            [(
                "🗓 Контент-план",
                f"cpev:content:{control._uuid_token(created_event_id)}:{token}",
            )]
        )
        event_rows.append(
            [(
                "✨ Сделать анонс",
                f"cpev:announce:{control._uuid_token(created_event_id)}:{token}",
            )]
        )
    event_rows.extend(
        [
            [(BACK_TO_EVENTS_LABEL, f"cpev:home:{token}")],
            [("🎥 Создать ещё", f"cpev:new:{token}")],
            [(BACK_TO_GROWTH_LABEL, f"cpo:content:{token}")],
        ]
    )
    await message.answer(
        event_creation_success_text(
            title=title,
            local_time=local_time,
            provider_key=created.provider_key,
            registration_url=registration_url,
            email_notifications_enabled=created.email_notifications_enabled,
            join_ready=join_ready,
        ),
        reply_markup=control._keyboard(event_rows),
    )


@router.message(ClientPlatformEventState.waiting_time)
async def receive_event_time(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    title = str(data.get("event_title") or "").strip()
    if not business_id or not title:
        await state.clear()
        await message.answer("Не удалось продолжить создание вебинара. Откройте вебинары заново.")
        return
    actor = await control._actor(int(message.from_user.id), business_id)
    actor.assert_can_manage_business()
    local_time = " ".join(str(message.text or "").strip().split())
    if local_time.casefold() in {"отмена", "cancel"}:
        await state.clear()
        await message.answer(
            "Создание вебинара отменено. Данные не изменены.",
            reply_markup=control._keyboard(
                [[(BACK_TO_EVENTS_LABEL, f"cpev:home:{control._uuid_token(business_id)}")]]
            ),
        )
        return
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
                join_url=None,
                offer_url=None,
            ),
        )
        registration_url = created.registration_url(_public_base_url())
    except (ValueError, RuntimeError):
        await message.answer(
            "Не удалось понять дату и время. Напишите, например: 16.09.2026 23:30",
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    await state.clear()
    token = control._uuid_token(business_id)
    created_event_id = str(getattr(created, "event_id", "") or "").strip()
    event_rows: list[list[tuple[str, str]]] = []
    if created_event_id:
        event_rows.append(
            [(
                "🔗 Добавить ссылку на эфир",
                f"cpev:join:{control._uuid_token(created_event_id)}:{token}",
            )]
        )
        event_rows.append(
            [(
                "✨ Сделать анонс",
                f"cpev:announce:{control._uuid_token(created_event_id)}:{token}",
            )]
        )
    event_rows.extend(
        [
            [(BACK_TO_EVENTS_LABEL, f"cpev:home:{token}")],
            [("🎥 Создать ещё", f"cpev:new:{token}")],
            [(BACK_TO_GROWTH_LABEL, f"cpo:content:{token}")],
        ]
    )
    await message.answer(
        event_creation_success_text(
            title=title,
            local_time=local_time,
            provider_key=created.provider_key,
            registration_url=registration_url,
            email_notifications_enabled=created.email_notifications_enabled,
            join_ready=False,
        ),
        reply_markup=control._keyboard(event_rows),
    )


@router.callback_query(F.data.startswith("cpev:announce:"))
async def create_event_announcement(callback: CallbackQuery) -> None:
    parts = str(callback.data or "").split(":", 3)
    if len(parts) != 4:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    event_id = control._token_uuid(parts[2])
    business_id = control._token_uuid(parts[3])
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        actor.assert_can_manage_business()
        draft = await draft_event_announcement(actor=actor, event_id=event_id)
        public_base = _public_base_url()
        telegram_url = draft.registration_url(
            public_base_url=public_base, source="telegram"
        )
        vk_url = draft.registration_url(public_base_url=public_base, source="vk")
        max_url = draft.registration_url(public_base_url=public_base, source="max")
        advertising_url = draft.registration_url(
            public_base_url=public_base, source="ads"
        )
    except (TenantPermissionDenied, ValueError, RuntimeError):
        await callback.answer("Не удалось подготовить анонс", show_alert=True)
        return
    business_token = control._uuid_token(business_id)
    source_note = (
        "Текст подготовлен AI и требует Вашего подтверждения перед публикацией."
        if draft.generated_by.startswith("ai:")
        else "Подготовлен безопасный текст. Перед публикацией его можно отредактировать в выбранном мессенджере."
    )
    await callback.answer("Анонс готов")
    await control._callback_message(callback).answer(
        "✨ Анонс готов\n\n"
        f"{draft.text}\n\n"
        f"{source_note}\n\n"
        f"🔗 Ссылка для рекламы:\n{advertising_url}\n\n"
        "Её можно вставить в рекламный кабинет, сайт или пост. ClientPlatform сохранит источник ads. "
        "Кнопки ниже используют отдельные ссылки регистрации для Telegram, VK и MAX.",
        reply_markup=_announcement_share_markup(
            text=draft.text,
            title=draft.title,
            telegram_url=telegram_url,
            vk_url=vk_url,
            max_url=max_url,
            business_token=business_token,
        ),
    )


@router.callback_query(F.data.startswith("cpev:join:"))
async def start_event_join_target(callback: CallbackQuery, state: FSMContext) -> None:
    parts = str(callback.data or "").split(":", 3)
    if len(parts) != 4:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    event_id = control._token_uuid(parts[2])
    business_id = control._token_uuid(parts[3])
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        actor.assert_can_manage_business()
    except TenantPermissionDenied:
        await callback.answer("Изменить ссылку может владелец или администратор", show_alert=True)
        return
    await state.clear()
    await state.set_state(ClientPlatformEventState.waiting_join_url)
    await state.update_data(event_business_id=business_id, event_id=event_id)
    await callback.answer()
    await control._callback_message(callback).answer(
        "🔗 Пришлите HTTPS-ссылку на эфир. После сохранения все персональные ссылки участников начнут вести на неё.",
        reply_markup=_cancel_keyboard(business_id),
    )


@router.message(ClientPlatformEventState.waiting_join_url)
async def receive_event_join_target(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("event_business_id") or "")
    event_id = str(data.get("event_id") or "")
    if not business_id or not event_id:
        await state.clear()
        await message.answer("Не удалось продолжить. Откройте вебинары заново.")
        return
    actor = await control._actor(int(message.from_user.id), business_id)
    try:
        updated = await asyncio.to_thread(
            set_event_join_target,
            actor=actor,
            event_id=event_id,
            join_url=str(message.text or "").strip(),
        )
    except (ValueError, RuntimeError):
        await message.answer(
            "Не удалось сохранить ссылку. Пришлите полный HTTPS-адрес площадки.",
            reply_markup=_cancel_keyboard(business_id),
        )
        return
    await state.clear()
    await message.answer(
        f"✅ Ссылка на эфир сохранена. Площадка: {updated.provider_key}. Персональные ссылки участников уже ведут на неё.",
        reply_markup=control._keyboard(
            [[(BACK_TO_EVENTS_LABEL, f"cpev:home:{control._uuid_token(business_id)}")]]
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
    "open_event_content_plan",
    "open_event_hub",
    "open_event_settings",
    "receive_custom_warmup_days",
    "receive_event_details",
    "receive_warmup_text",
    "router",
    "start_event_wizard",
    "toggle_event_followup_channel",
    "toggle_event_followup_segment",
    "toggle_event_followups",
]

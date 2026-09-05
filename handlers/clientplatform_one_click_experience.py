from __future__ import annotations

"""One primary owner action with safe automatic orchestration."""

import asyncio
from types import ModuleType
from urllib.parse import urlencode

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from clientplatform.application.ad_connections import (
    ad_connections_enabled,
    create_managed_ad_publication_draft,
    list_ad_connections,
    list_ad_publications,
    start_yandex_direct_oauth,
    yandex_direct_provider_configured,
)
from clientplatform.application.promotions import create_slot_promotion, promotion_public_url
from clientplatform.domain.ad_connections import AdConnectionError, AdConnectionStatus
from clientplatform.domain.bookings import BookingSlotStatus
from clientplatform.domain.promotions import PromotionChannel, PromotionError
from clientplatform.domain.tenancy import PlatformRole, TenantPermissionDenied
from clientplatform.integrations.yandex_direct import YandexDirectError
from clientplatform.presentation import owner_navigation as nav
from clientplatform.runtime.cockpit_links import cockpit_web_app_url
from config.settings import settings

from . import clientplatform_ad_connections as ad
from . import clientplatform_control as control
from . import clientplatform_goal_first_safety as goal_contract
from . import clientplatform_simple_experience as simple

router = Router(name="clientplatform_one_click_experience")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

_SETTINGS_MESSENGER_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.SUPPORT,
    }
)
_SETTINGS_SYSTEM_ROLES = frozenset({PlatformRole.OWNER, PlatformRole.ADMINISTRATOR})
_SETTINGS_TEAM_ROLES = frozenset({PlatformRole.OWNER})


_REGIONS = {
    "47": (47,),
    "нижний новгород": (47,),
    "213": (213,),
    "москва": (213,),
    "2": (2,),
    "санкт-петербург": (2,),
    "санкт петербург": (2,),
    "спб": (2,),
}


class OneClickOwnerState(StatesGroup):
    selecting_connection = State()
    waiting_region = State()


def _home_keyboard(business_id: str) -> InlineKeyboardMarkup:
    token = control._uuid_token(business_id)
    return control._keyboard(
        [
            [(goal_contract.ACQUIRE_CLIENTS.label, goal_contract.ACQUIRE_CLIENTS.callback(token))],
            [
                ("👥 Клиенты и запись", f"cpj:bookings:{token}"),
                ("⚙️ Ещё", f"cpo:more:{token}"),
            ],
        ]
    )


async def send_one_click_dashboard(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    _actor, access, _profile, _caps, _customers, _programs, slots = (
        await simple._business_snapshot(user_id=user_id, business_id=business_id)
    )
    open_count = sum(item.slot.status == BookingSlotStatus.OPEN for item in slots)
    status = (
        f"Свободных времён для записи: {open_count}."
        if open_count
        else "Свободного времени пока нет — помогу открыть его по ходу."
    )
    await message.answer(
        f"🏠 {access.business.name}\n\n"
        f"Нажмите «{goal_contract.ACQUIRE_CLIENTS.label}». Я сам проверю свободное время, рекламу "
        "и прежние настройки. Спрошу только то, что нельзя определить безопасно.\n\n"
        f"{status}",
        reply_markup=_home_keyboard(business_id),
    )


def _target(event: CallbackQuery | Message) -> Message:
    return event if isinstance(event, Message) else control._callback_message(event)


def _user_id(event: CallbackQuery | Message) -> int:
    if event.from_user is None:
        raise ValueError("Telegram user is required")
    return int(event.from_user.id)


def _recent(jobs, *, connection_id: str):
    return next(
        (
            item
            for item in jobs
            if str(getattr(item, "connection_id", "")) == connection_id
        ),
        None,
    )


def _indexed_choice(data: dict, key: str, callback_data: str | None):
    raw_index = str(callback_data or "").rsplit(":", 1)[-1]
    if not raw_index.isdigit():
        return None
    values = data.get(key)
    if not isinstance(values, list):
        return None
    index = int(raw_index)
    if index >= len(values):
        return None
    return values[index]


def _acquisition_link(source_token: str) -> str:
    """Build the canonical public destination independently from owner transport."""

    public_base = str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip()
    if not public_base:
        raise RuntimeError("public acquisition base URL is not configured")
    return promotion_public_url(base_url=public_base, source_token=source_token)


async def _reload_slot(actor, slot_id: str):
    slots = await asyncio.to_thread(control.list_booking_slots, actor=actor)
    return next(
        (
            item
            for item in slots
            if str(item.slot.id) == slot_id
            and item.slot.status == BookingSlotStatus.OPEN
        ),
        None,
    )


async def _fallback_failure(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    business_token: str,
    reason: str,
) -> None:
    await state.clear()
    await control._callback_message(callback).answer(
        f"{reason}\n\nНе удалось собрать запасной вариант автоматически.",
        reply_markup=control._keyboard(
            [[("🏠 В кабинет", f"cpj:home:{business_token}")]]
        ),
    )


async def _fallback(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    actor,
    business_token: str,
    slot,
    reason: str,
) -> None:
    if slot is None:
        await state.clear()
        await control._callback_message(callback).answer(
            "Свободное время уже изменилось. Проверю всё заново по одной кнопке.",
            reply_markup=control._keyboard(
                [[("🔄 Проверить снова", goal_contract.ACQUIRE_CLIENTS.callback(business_token))]]
            ),
        )
        return
    try:
        view = await asyncio.to_thread(
            create_slot_promotion,
            actor=actor,
            slot_id=slot.slot.id,
            channel=PromotionChannel.WEBSITE,
        )
    except (PromotionError, TenantPermissionDenied):
        await _fallback_failure(
            callback,
            state,
            business_token=business_token,
            reason=reason,
        )
        return
    try:
        link = _acquisition_link(view.campaign.source_token)
    except (RuntimeError, ValueError):
        await _fallback_failure(
            callback,
            state,
            business_token=business_token,
            reason=reason,
        )
        return
    creative = view.campaign.creative
    text = f"{creative.headline}\n\n{creative.primary_text}\n\n{creative.description}"
    share_url = "https://t.me/share/url?" + urlencode({"url": link, "text": text})
    await state.clear()
    await control._callback_message(callback).answer(
        "✅ Уже можно привлекать клиентов\n\n"
        f"{reason}\n\n"
        f"Я выбрал ближайшее свободное время: {slot.local_start} · "
        f"{slot.offering_title}. Готовы текст и измеряемая ссылка.\n\n"
        f"{text}\n\nЗаписаться: {link}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📨 Отправить объявление", url=share_url)],
                [
                    InlineKeyboardButton(
                        text="🔄 Проверить Яндекс",
                        callback_data=goal_contract.ACQUIRE_CLIENTS.callback(business_token),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🏠 В кабинет",
                        callback_data=f"cpj:home:{business_token}",
                    )
                ],
            ]
        ),
    )


async def _draft_failure(
    event: CallbackQuery | Message,
    state: FSMContext,
    *,
    business_token: str,
) -> None:
    await state.clear()
    await _target(event).answer(
        "Не удалось подготовить рекламный черновик. "
        "Ничего не запущено и деньги не списывались.",
        reply_markup=control._keyboard(
            [
                [("🔄 Попробовать снова", goal_contract.ACQUIRE_CLIENTS.callback(business_token))],
                [("🏠 В кабинет", f"cpj:home:{business_token}")],
            ]
        ),
    )


async def _prepare_draft(
    event: CallbackQuery | Message,
    state: FSMContext,
    *,
    data: dict,
    region_ids: tuple[int, ...],
) -> None:
    actor = await control._actor(_user_id(event), str(data["business_id"]))
    try:
        promotion = await asyncio.to_thread(
            create_slot_promotion,
            actor=actor,
            slot_id=str(data["slot_id"]),
            channel=PromotionChannel.WEBSITE,
        )
    except (PromotionError, TenantPermissionDenied):
        await _draft_failure(
            event,
            state,
            business_token=str(data["business_token"]),
        )
        return
    try:
        source_url = _acquisition_link(promotion.campaign.source_token)
    except (RuntimeError, ValueError):
        await _draft_failure(
            event,
            state,
            business_token=str(data["business_token"]),
        )
        return
    try:
        draft = await asyncio.to_thread(
            create_managed_ad_publication_draft,
            actor=actor,
            promotion_campaign_id=promotion.campaign.id,
            connection_id=str(data["connection_id"]),
            region_ids=region_ids,
            source_url=source_url,
        )
    except (AdConnectionError, TenantPermissionDenied, YandexDirectError):
        await _draft_failure(
            event,
            state,
            business_token=str(data["business_token"]),
        )
        return
    await state.set_state(ad.AdConnectionState.confirming_publication)
    await state.set_data(
        {
            **data,
            "promotion_campaign_id": promotion.campaign.id,
            "source_url": source_url,
            "job_id": draft.job.id,
            "creative_title": draft.job.title,
            "creative_body": draft.job.text,
            "creative_job_id": "",
        }
    )
    await _target(event).answer(
        "✅ Реклама подготовлена\n\n"
        f"Заголовок: {draft.job.title}\n"
        f"Текст: {draft.job.text}\n\n"
        "ClientPlatform сам выделил этому продвижению отдельную кампанию Яндекса. "
        "Ничего ещё не запущено: показов, модерации и расходов нет.",
        reply_markup=control._keyboard(
            [
                [("🖼 Создать красивую картинку", "cpa:creative:image")],
                [(ad._CONFIRM_DRAFT_LABEL, "cpa:confirm")],
                [("✏️ Изменить вручную", f"cpa:promote:{data['business_token']}")],
                [("🏠 В кабинет", f"cpj:home:{data['business_token']}")],
            ]
        ),
    )


async def _choose_connection(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    data: dict,
    connection_id: str,
) -> None:
    actor = await control._actor(int(callback.from_user.id), str(data["business_id"]))
    try:
        jobs = await asyncio.to_thread(list_ad_publications, actor=actor)
    except (AdConnectionError, TenantPermissionDenied):
        jobs = []
    saved = _recent(jobs, connection_id=connection_id)
    next_data = {**data, "connection_id": connection_id}
    regions = tuple(getattr(saved, "region_ids", ()) or ()) if saved else ()
    if regions:
        await _prepare_draft(callback, state, data=next_data, region_ids=regions)
        return
    await state.set_state(OneClickOwnerState.waiting_region)
    await state.set_data(next_data)
    await control._callback_message(callback).answer(
        "Осталось только указать регион — это нельзя безопасно угадать. "
        "Рекламную кампанию ClientPlatform создаст и привяжет сам; "
        "в следующий раз прежний регион тоже возьму автоматически.",
        reply_markup=control._keyboard(
            [
                [("Нижний Новгород", "cpo:region:47"), ("Москва", "cpo:region:213")],
                [("Санкт-Петербург", "cpo:region:2")],
                [("Другой регион", "cpo:region:other")],
                [("🏠 Отмена", f"cpj:home:{data['business_token']}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cpo:start:"))
async def get_clients_one_click(callback: CallbackQuery, state: FSMContext) -> None:
    token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    await callback.answer("Готовлю всё сам…")
    await state.clear()
    slots = await asyncio.to_thread(control.list_booking_slots, actor=actor)
    open_slots = [item for item in slots if item.slot.status == BookingSlotStatus.OPEN]
    if not open_slots:
        await control._callback_message(callback).answer(
            "Сначала нужно одно свободное время. Нажмите кнопку — остальное поведу сам.",
            reply_markup=control._keyboard(
                [
                    [("➕ Открыть время", f"cps:firstbook:{token}")],
                    [(nav.BACK.label, f"cpj:home:{token}")],
                ]
            ),
        )
        return
    slot = min(open_slots, key=lambda item: item.slot.starts_at)
    data = {
        "business_id": business_id,
        "business_token": token,
        "slot_id": str(slot.slot.id),
    }
    await state.set_data(data)
    if not ad_connections_enabled() or not yandex_direct_provider_configured():
        await _fallback(
            callback,
            state,
            actor=actor,
            business_token=token,
            slot=slot,
            reason="Яндекс-реклама пока не включена на платформе.",
        )
        return
    try:
        connections, jobs = await asyncio.gather(
            asyncio.to_thread(list_ad_connections, actor=actor),
            asyncio.to_thread(list_ad_publications, actor=actor),
        )
    except TenantPermissionDenied:
        await _fallback(
            callback,
            state,
            actor=actor,
            business_token=token,
            slot=slot,
            reason="У этой роли нет доступа к личному рекламному кабинету, но продвижение доступно.",
        )
        return
    except AdConnectionError:
        await _fallback(
            callback,
            state,
            actor=actor,
            business_token=token,
            slot=slot,
            reason="Рекламный кабинет временно недоступен.",
        )
        return
    active = [item for item in connections if item.status == AdConnectionStatus.ACTIVE]
    if not active:
        try:
            oauth = await asyncio.to_thread(start_yandex_direct_oauth, actor=actor)
        except (AdConnectionError, YandexDirectError, TenantPermissionDenied):
            await _fallback(
                callback,
                state,
                actor=actor,
                business_token=token,
                slot=slot,
                reason="Подключение Яндекс Директа сейчас недоступно.",
            )
            return
        await state.clear()
        await control._callback_message(callback).answer(
            "Один раз подключите Яндекс. Пароль остаётся у Яндекса.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔐 Подключить Яндекс",
                            url=oauth.authorization_url,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🏠 В кабинет",
                            callback_data=f"cpj:home:{token}",
                        )
                    ],
                ]
            ),
        )
        return
    previous = next(
        (
            str(job.connection_id)
            for job in jobs
            if any(str(item.id) == str(job.connection_id) for item in active)
        ),
        "",
    )
    selected = next((item for item in active if str(item.id) == previous), None)
    if selected is None and len(active) == 1:
        selected = active[0]
    if selected is not None:
        await _choose_connection(
            callback,
            state,
            data=data,
            connection_id=str(selected.id),
        )
        return
    await state.set_state(OneClickOwnerState.selecting_connection)
    await state.set_data(
        {**data, "connection_ids": [str(item.id) for item in active]}
    )
    await control._callback_message(callback).answer(
        "Подключено несколько кабинетов. Выберите нужный один раз.",
        reply_markup=control._keyboard(
            [
                [
                    (f"Яндекс · {item.external_login}", f"cpo:connection:{index}")
                ]
                for index, item in enumerate(active)
            ]
            + [[("🏠 Отмена", f"cpj:home:{token}")]],
        ),
    )


@router.callback_query(
    OneClickOwnerState.selecting_connection,
    F.data.startswith("cpo:connection:"),
)
async def choose_one_click_connection(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    connection_id = _indexed_choice(data, "connection_ids", callback.data)
    if connection_id is None:
        await callback.answer("Кнопка устарела. Начните ещё раз.", show_alert=True)
        return
    await callback.answer("Продолжаю…")
    await _choose_connection(
        callback,
        state,
        data=data,
        connection_id=str(connection_id),
    )


@router.callback_query(
    OneClickOwnerState.waiting_region,
    F.data.startswith("cpo:region:"),
)
async def choose_one_click_region(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    raw = str(callback.data).split(":", 2)[2]
    if raw == "other":
        await callback.answer()
        await control._callback_message(callback).answer(
            "Напишите город: Москва, Нижний Новгород, Санкт-Петербург. "
            "Для другого города можно указать ID региона Яндекс Директа."
        )
        return
    regions = _REGIONS.get(raw)
    if regions is None:
        await callback.answer("Регион не найден", show_alert=True)
        return
    await callback.answer("Готовлю черновик…")
    await _prepare_draft(callback, state, data=data, region_ids=regions)


@router.message(OneClickOwnerState.waiting_region)
async def receive_one_click_region(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    raw = " ".join(str(message.text or "").strip().lower().split())
    regions = _REGIONS.get(raw)
    if regions is None:
        try:
            regions = tuple(
                int(item.strip()) for item in raw.split(",") if item.strip()
            )
            if not regions or any(item <= 0 for item in regions):
                raise ValueError
        except ValueError:
            await message.answer("Напишите город или ID региона Яндекс Директа.")
            return
    await _prepare_draft(message, state, data=data, region_ids=regions)



def _allowed(actor, check) -> bool:
    try:
        check()
    except TenantPermissionDenied:
        return False
    return True


def _more_rows(token: str, actor) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    if _allowed(actor, actor.assert_can_view_outcome_ledger):
        rows.append([(nav.MONEY_RESULT.label, f"cpg:period:{token}:7")])
    can_customers = _allowed(actor, actor.assert_can_view_customer_records)
    if can_customers:
        rows.append([(nav.CLIENTS_SALES.label, f"cpo:clients:{token}")])
    if can_customers or _allowed(actor, actor.assert_can_manage_programs):
        rows.append([(nav.SERVICES_BOOKING.label, f"cpo:work:{token}")])
    if _allowed(actor, actor.assert_can_manage_promotions):
        rows.append([(nav.CONTENT_PROMOTION.label, f"cpo:content:{token}")])
    rows.append([(nav.BUSINESS_SETTINGS.label, f"cpo:settings:{token}")])
    rows.append([(nav.BACK.label, f"cpj:home:{token}")])
    return rows


def _more_keyboard(token: str, actor) -> InlineKeyboardMarkup:
    quick_actions = control._keyboard(_more_rows(token, actor))
    cockpit_url = cockpit_web_app_url()
    if cockpit_url is None:
        return quick_actions
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏠 Открыть кабинет",
                    web_app=WebAppInfo(url=cockpit_url),
                )
            ],
            *quick_actions.inline_keyboard,
        ]
    )


def _client_tools_rows(token: str, actor) -> tuple[list[list[tuple[str, str]]], list[str]]:
    allowed = _allowed(actor, actor.assert_can_view_customer_records)
    rows: list[list[tuple[str, str]]] = []
    help_lines: list[str] = []
    if allowed:
        rows.append([("💬 Обращения и продажи", f"cps:s:{token}")])
        help_lines.append("• ответить на новую заявку или продолжить продажу → «💬 Обращения и продажи»")
    if allowed:
        rows.append([("📅 Записи клиентов", f"cpj:bookings:{token}")])
        help_lines.append("• посмотреть, кто и когда записан → «📅 Записи клиентов»")
    if allowed:
        rows.append([("🔎 Все клиенты", f"cpa:{token}:customer-list")])
        help_lines.append("• найти конкретного человека → «🔎 Все клиенты»")
    rows.append([(nav.BACK.label, f"cpo:more:{token}")])
    return rows, help_lines


async def _send_client_tools(message: Message, *, token: str, actor) -> None:
    rows, help_lines = _client_tools_rows(token, actor)
    body = "\n".join(help_lines) or "Для Вашей роли здесь сейчас нет доступных действий."
    await message.answer(
        "👥 Клиенты и продажи\n\nЕсли Вам нужно:\n" + body,
        reply_markup=control._keyboard(rows),
    )


def _content_tools_rows(token: str, actor) -> tuple[list[list[tuple[str, str]]], list[str]]:
    rows: list[list[tuple[str, str]]] = []
    help_lines: list[str] = []
    if _allowed(actor, actor.assert_can_manage_promotions):
        rows.extend(
            [
                [("📣 Публикации", f"cpa:{token}:publications")],
                [(nav.COPY.label, f"cpa:{token}:copy")],
                [(nav.OFFERS.label, f"cpa:{token}:offers")],
                [("📣 Реклама", f"cpo:ads:{token}")],
                [("🤝 Партнёрства", f"cpg:home:{token}")],
            ]
        )
        help_lines.extend(
            [
                "• создать или запланировать пост → «📣 Публикации»",
                f"• подготовить текст → «{nav.COPY.label}»",
                f"• проверить, что именно Вы предлагаете → «{nav.OFFERS.label}»",
                "• запустить продвижение → «📣 Реклама»",
                "• привлекать через партнёров → «🤝 Партнёрства»",
            ]
        )
    rows.append([(nav.BACK.label, f"cpo:more:{token}")])
    return rows, help_lines

async def _send_content_tools(message: Message, *, token: str, actor) -> None:
    rows, help_lines = _content_tools_rows(token, actor)
    body = "\n".join(help_lines) or "Для Вашей роли здесь сейчас нет доступных действий."
    await message.answer(
        "📈 Продвижение и контент\n\nЕсли Вам нужно:\n" + body,
        reply_markup=control._keyboard(rows),
    )


def _settings_rows(token: str, actor) -> tuple[list[list[tuple[str, str]]], list[str]]:
    rows: list[list[tuple[str, str]]] = []
    help_lines: list[str] = []
    if actor.role in _SETTINGS_MESSENGER_ROLES:
        rows.append([(nav.MESSENGERS.label, f"cpa:{token}:messengers")])
        help_lines.append(f"• подключить или проверить Telegram, ВКонтакте или MAX → «{nav.MESSENGERS.label}»")
    rows.append([("🧩 Бизнес и возможности", f"cps:advanced:{token}")])
    help_lines.append("• посмотреть услуги и возможности бизнеса → «🧩 Бизнес и возможности»")
    if actor.role in _SETTINGS_TEAM_ROLES:
        rows.append([("👤 Сотрудники и доступы", f"cpa:{token}:menu-team")])
        help_lines.append("• добавить сотрудника или изменить доступ → «👤 Сотрудники и доступы»")
    if actor.role in _SETTINGS_SYSTEM_ROLES:
        rows.append([("🛠 Технические проверки", f"cpa:{token}:menu-system")])
        help_lines.append("• проверить техническое состояние → «🛠 Технические проверки»")
    rows.append([(nav.BACK.label, f"cpo:more:{token}")])
    return rows, help_lines

async def _send_settings_tools(message: Message, *, token: str, actor) -> None:
    rows, help_lines = _settings_rows(token, actor)
    body = "\n".join(help_lines) or "Для Вашей роли здесь сейчас нет доступных настроек."
    await message.answer(
        "⚙️ Настройки бизнеса\n\nЕсли Вам нужно:\n" + body,
        reply_markup=control._keyboard(rows),
    )


async def _send_work_tools(message: Message, *, token: str, actor) -> None:
    if not (
        _allowed(actor, actor.assert_can_view_customer_records)
        or _allowed(actor, actor.assert_can_manage_programs)
    ):
        await message.answer(
            "📅 Услуги и запись\n\nДля Вашей роли этот раздел недоступен.",
            reply_markup=control._keyboard([[(nav.BACK.label, f"cpo:more:{token}")]]),
        )
        return
    await message.answer(
        "📅 Услуги и запись\n\n"
        "Если Вам нужно:\n"
        "• настроить то, что можно заказать → «🧰 Мои услуги»\n"
        "• открыть или проверить время → «📅 Мой календарь»\n"
        "• посмотреть ссылку для клиентов → «🔗 Моя страница»",
        reply_markup=control._keyboard(
            [
                [("🧰 Мои услуги", f"cpj:services:{token}")],
                [("📅 Мой календарь", f"cpj:calendar:{token}:30")],
                [("🔗 Моя страница", f"cpj:page:{token}")],
                [(nav.BACK.label, f"cpo:more:{token}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cpo:more:"))
async def open_more(callback: CallbackQuery) -> None:
    token = str(callback.data).split(":", 2)[2]
    actor = await control._actor(int(callback.from_user.id), control._token_uuid(token))
    await control._callback_message(callback).answer(
        "🏠 Кабинет ClientPlatform\n\n"
        "Откройте кабинет, чтобы увидеть главное по бизнесу в одном месте. "
        "Ниже показаны только те быстрые действия, которые доступны Вашей роли.",
        reply_markup=_more_keyboard(token, actor),
    )


@router.callback_query(F.data.startswith("cpo:clients:"))
async def open_client_tools(callback: CallbackQuery) -> None:
    token = str(callback.data).split(":", 2)[2]
    actor = await control._actor(int(callback.from_user.id), control._token_uuid(token))
    await _send_client_tools(control._callback_message(callback), token=token, actor=actor)


@router.callback_query(F.data.startswith("cpo:content:"))
async def open_content_tools(callback: CallbackQuery) -> None:
    token = str(callback.data).split(":", 2)[2]
    actor = await control._actor(int(callback.from_user.id), control._token_uuid(token))
    await _send_content_tools(control._callback_message(callback), token=token, actor=actor)


@router.callback_query(F.data.startswith("cpo:settings:"))
async def open_settings_tools(callback: CallbackQuery) -> None:
    token = str(callback.data).split(":", 2)[2]
    actor = await control._actor(int(callback.from_user.id), control._token_uuid(token))
    await _send_settings_tools(control._callback_message(callback), token=token, actor=actor)


@router.callback_query(F.data.startswith("cpo:work:"))
async def open_work_tools(callback: CallbackQuery) -> None:
    token = str(callback.data).split(":", 2)[2]
    actor = await control._actor(int(callback.from_user.id), control._token_uuid(token))
    await _send_work_tools(control._callback_message(callback), token=token, actor=actor)


async def send_one_click_section(
    message: Message,
    *,
    user_id: int,
    business_id: str,
    section: str,
) -> None:
    """Open a Cockpit section through existing canonical Telegram surfaces."""

    actor = await control._actor(user_id, business_id)
    normalized = str(section or "").strip().lower()
    token = control._uuid_token(business_id)
    if normalized == "calendar":
        actor.assert_can_view_customer_records()
        await message.answer(
            "📅 Календарь и записи\n\nОткройте актуальные записи бизнеса.",
            reply_markup=control._keyboard(
                [[("📅 Записи клиентов", f"cpj:bookings:{token}")], [(nav.HOME.label, f"cpj:home:{token}")]]
            ),
        )
        return
    if normalized == "sales":
        actor.assert_can_view_customer_records()
        await _send_client_tools(message, token=token, actor=actor)
        return
    if normalized == "content":
        actor.assert_can_view_programs()
        rows: list[list[tuple[str, str]]] = [[("📚 Материалы и программы", f"cp:cprograms:{token}")]]
        if _allowed(actor, actor.assert_can_manage_promotions):
            rows.append([("📣 Публикации и продвижение", f"cpo:content:{token}")])
        rows.append([(nav.HOME.label, f"cpj:home:{token}")])
        await message.answer(
            "📚 Контент и материалы\n\nПоказаны только действия, доступные Вашей роли.",
            reply_markup=control._keyboard(rows),
        )
        return
    if normalized in {"growth", "analytics"}:
        actor.assert_can_view_promotion_analytics()
        rows = []
        if _allowed(actor, actor.assert_can_view_outcome_ledger):
            rows.append([(nav.MONEY_RESULT.label, f"cpg:period:{token}:7")])
        rows.append([("📊 Результаты Яндекс", f"cpy:a:{token}:30")])
        rows.append([("🧪 A/B креативы", f"cpa:{token}:experiments")])
        if _allowed(actor, actor.assert_can_manage_promotions):
            rows.append([("📣 Реклама и продвижение", f"cpo:ads:{token}")])
        rows.append([(nav.HOME.label, f"cpj:home:{token}")])
        await message.answer(
            "📈 Рост и аналитика\n\nПоказаны только доступные для Вашей роли данные и действия.",
            reply_markup=control._keyboard(rows),
        )
        return
    if normalized == "automation":
        actor.assert_can_manage_business()
        await message.answer(
            "🤖 Автоматизация\n\nЗдесь можно проверить разрешённые системе действия и изменить границы автоматизации.",
            reply_markup=control._keyboard(
                [[("🤖 Открыть автоматизацию", f"cpa:{token}:autopilot")], [(nav.HOME.label, f"cpj:home:{token}")]]
            ),
        )
        return
    if normalized == "connections":
        actor.assert_can_manage_business()
        await message.answer(
            "💬 Подключения\n\nПодключите или проверьте клиентские мессенджеры бизнеса.",
            reply_markup=control._keyboard(
                [[(nav.MESSENGERS.label, f"cpa:{token}:messengers")], [(nav.HOME.label, f"cpj:home:{token}")]]
            ),
        )
        return
    if normalized == "team":
        if actor.role != PlatformRole.OWNER:
            raise TenantPermissionDenied("team controls are owner-only in the current UI")
        await message.answer(
            "👤 Команда и роли\n\nУправляйте сотрудниками и их доступами.",
            reply_markup=control._keyboard(
                [[("👤 Сотрудники и доступы", f"cpa:{token}:menu-team")], [(nav.HOME.label, f"cpj:home:{token}")]]
            ),
        )
        return
    if normalized == "settings":
        actor.assert_can_manage_business()
        await _send_settings_tools(message, token=token, actor=actor)
        return
    raise ValueError("unsupported cockpit section")

@router.callback_query(F.data.startswith("cpo:ads:"))
async def open_ad_tools(callback: CallbackQuery) -> None:
    token = str(callback.data).split(":", 2)[2]
    await control._actor(int(callback.from_user.id), control._token_uuid(token))
    await control._callback_message(callback).answer(
        f"📣 Реклама и продвижение\n\nОбычный путь — «{goal_contract.ACQUIRE_CLIENTS.label}».",
        reply_markup=control._keyboard(
            [
                [(goal_contract.ACQUIRE_CLIENTS.label, goal_contract.ACQUIRE_CLIENTS.callback(token))],
                [("📣 Яндекс Директ", f"cpa:home:{token}")],
                [("📊 Результаты Яндекс", f"cpy:a:{token}:30")],
                [("📣 Партнёрские материалы", f"cpg:materials:{token}")],
                [(nav.BACK.label, f"cpo:more:{token}")],
            ]
        ),
    )


def install_one_click_experience(
    *,
    owner_module: ModuleType,
    simple_module: ModuleType,
    control_module: ModuleType,
) -> None:
    if bool(getattr(owner_module, "_one_click_experience_installed", False)):
        return
    owner_module._owner_keyboard = _home_keyboard
    owner_module.send_owner_dashboard = send_one_click_dashboard
    simple_module.send_simple_dashboard = send_one_click_dashboard
    control_module._send_dashboard = send_one_click_dashboard
    owner_module._one_click_experience_installed = True


__all__ = [
    "OneClickOwnerState",
    "get_clients_one_click",
    "install_one_click_experience",
    "router",
    "send_one_click_dashboard",
    "send_one_click_section",
]

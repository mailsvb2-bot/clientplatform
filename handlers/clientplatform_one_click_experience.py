from __future__ import annotations

"""One primary owner action with safe automatic orchestration."""

import asyncio
from types import ModuleType
from urllib.parse import urlencode

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.ad_connections import (
    ad_connections_enabled,
    create_ad_publication_draft,
    list_ad_connections,
    list_ad_publications,
    list_yandex_direct_campaigns,
    start_yandex_direct_oauth,
    yandex_direct_provider_configured,
)
from clientplatform.application.promotions import create_slot_promotion, promotion_start_payload
from clientplatform.domain.ad_connections import AdConnectionError, AdConnectionStatus
from clientplatform.domain.bookings import BookingSlotStatus
from clientplatform.domain.promotions import PromotionChannel, PromotionError
from clientplatform.domain.tenancy import TenantPermissionDenied
from clientplatform.integrations.yandex_direct import YandexDirectError

from . import clientplatform_ad_connections as ad
from . import clientplatform_control as control
from . import clientplatform_simple_experience as simple

router = Router(name="clientplatform_one_click_experience")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

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
    selecting_campaign = State()
    waiting_region = State()


def _home_keyboard(business_id: str) -> InlineKeyboardMarkup:
    token = control._uuid_token(business_id)
    return control._keyboard(
        [
            [("🚀 Получить клиентов", f"cpo:start:{token}")],
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
        "Нажмите «🚀 Получить клиентов». Я сам проверю свободное время, рекламу "
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


def _eligible(campaigns):
    return [
        item
        for item in campaigns
        if str(getattr(item, "state", "")).strip().upper() == "ON"
        and str(getattr(item, "status", "")).strip().upper() == "ACCEPTED"
    ]


def _recent(jobs, *, connection_id: str, campaign_id: str = ""):
    return next(
        (
            item
            for item in jobs
            if str(getattr(item, "connection_id", "")) == connection_id
            and (
                not campaign_id
                or str(getattr(item, "external_campaign_id", "")) == campaign_id
            )
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


async def _username(event: CallbackQuery | Message) -> str:
    username = str(getattr(await event.bot.get_me(), "username", "") or "").strip()
    if not username:
        raise RuntimeError("bot username missing")
    return username


def _link(username: str, source_token: str) -> str:
    return f"https://t.me/{username}?start={promotion_start_payload(source_token)}"


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
                [[("🔄 Проверить снова", f"cpo:start:{business_token}")]]
            ),
        )
        return
    try:
        view = await asyncio.to_thread(
            create_slot_promotion,
            actor=actor,
            slot_id=slot.slot.id,
            channel=PromotionChannel.TELEGRAM,
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
        link = _link(await _username(callback), view.campaign.source_token)
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
                        callback_data=f"cpo:start:{business_token}",
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
                [("🔄 Попробовать снова", f"cpo:start:{business_token}")],
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
        source_url = _link(await _username(event), promotion.campaign.source_token)
    except (RuntimeError, ValueError):
        await _draft_failure(
            event,
            state,
            business_token=str(data["business_token"]),
        )
        return
    try:
        draft = await asyncio.to_thread(
            create_ad_publication_draft,
            actor=actor,
            promotion_campaign_id=promotion.campaign.id,
            connection_id=str(data["connection_id"]),
            external_campaign_id=str(data["external_campaign_id"]),
            external_campaign_name=str(data["external_campaign_name"]),
            region_ids=region_ids,
            source_url=source_url,
        )
    except (AdConnectionError, TenantPermissionDenied):
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
        f"Кампания: {draft.campaign_name}\n"
        f"Заголовок: {draft.job.title}\n"
        f"Текст: {draft.job.text}\n\n"
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


async def _choose_campaign(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    data: dict,
    campaign_id: str,
    campaign_name: str,
) -> None:
    actor = await control._actor(int(callback.from_user.id), str(data["business_id"]))
    try:
        jobs = await asyncio.to_thread(list_ad_publications, actor=actor)
    except (AdConnectionError, TenantPermissionDenied):
        jobs = []
    saved = _recent(
        jobs,
        connection_id=str(data["connection_id"]),
        campaign_id=campaign_id,
    )
    next_data = {
        **data,
        "external_campaign_id": campaign_id,
        "external_campaign_name": campaign_name,
    }
    regions = tuple(getattr(saved, "region_ids", ()) or ()) if saved else ()
    if regions:
        await _prepare_draft(callback, state, data=next_data, region_ids=regions)
        return
    await state.set_state(OneClickOwnerState.waiting_region)
    await state.set_data(next_data)
    await control._callback_message(callback).answer(
        "Осталось только указать регион — это нельзя безопасно угадать. "
        "В следующий раз ClientPlatform возьмёт прежний выбор сам.",
        reply_markup=control._keyboard(
            [
                [("Нижний Новгород", "cpo:region:47"), ("Москва", "cpo:region:213")],
                [("Санкт-Петербург", "cpo:region:2")],
                [("Другой регион", "cpo:region:other")],
                [("🏠 Отмена", f"cpj:home:{data['business_token']}")],
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
        campaigns = await asyncio.wait_for(
            asyncio.to_thread(
                list_yandex_direct_campaigns,
                actor=actor,
                connection_id=connection_id,
            ),
            timeout=25.0,
        )
    except (asyncio.TimeoutError, AdConnectionError, YandexDirectError):
        await _fallback(
            callback,
            state,
            actor=actor,
            business_token=str(data["business_token"]),
            slot=await _reload_slot(actor, str(data["slot_id"])),
            reason="Яндекс сейчас не ответил. Это не должно останавливать работу.",
        )
        return
    except TenantPermissionDenied:
        await _fallback(
            callback,
            state,
            actor=actor,
            business_token=str(data["business_token"]),
            slot=await _reload_slot(actor, str(data["slot_id"])),
            reason="Для этой роли личный рекламный кабинет недоступен.",
        )
        return
    eligible = _eligible(campaigns)
    if not eligible:
        await _fallback(
            callback,
            state,
            actor=actor,
            business_token=str(data["business_token"]),
            slot=await _reload_slot(actor, str(data["slot_id"])),
            reason="Кампания Яндекса пока не готова: возможны оплата, модерация или пауза.",
        )
        return
    try:
        jobs = await asyncio.to_thread(list_ad_publications, actor=actor)
    except (AdConnectionError, TenantPermissionDenied):
        jobs = []
    previous = _recent(jobs, connection_id=connection_id)
    previous_id = str(getattr(previous, "external_campaign_id", "")) if previous else ""
    selected = next(
        (item for item in eligible if str(item.campaign_id) == previous_id),
        None,
    )
    if selected is None and len(eligible) == 1:
        selected = eligible[0]
    next_data = {
        **data,
        "connection_id": connection_id,
        "campaigns": [
            {"id": str(item.campaign_id), "name": str(item.name)}
            for item in eligible
        ],
    }
    if selected is not None:
        await _choose_campaign(
            callback,
            state,
            data=next_data,
            campaign_id=str(selected.campaign_id),
            campaign_name=str(selected.name),
        )
        return
    await state.set_state(OneClickOwnerState.selecting_campaign)
    await state.set_data(next_data)
    await control._callback_message(callback).answer(
        "Нашёл несколько готовых кампаний. Выберите одну — дальше всё сделаю сам.",
        reply_markup=control._keyboard(
            [
                [(item.name[:45], f"cpo:campaign:{index}")]
                for index, item in enumerate(eligible[:20])
            ]
            + [[("🏠 Отмена", f"cpj:home:{data['business_token']}")]],
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
                    [("🏠 В кабинет", f"cpj:home:{token}")],
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
    OneClickOwnerState.selecting_campaign,
    F.data.startswith("cpo:campaign:"),
)
async def choose_one_click_campaign(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    selected = _indexed_choice(data, "campaigns", callback.data)
    if not isinstance(selected, dict):
        await callback.answer("Кнопка устарела. Начните ещё раз.", show_alert=True)
        return
    campaign_id = str(selected.get("id") or "").strip()
    campaign_name = str(selected.get("name") or "").strip()
    if not campaign_id or not campaign_name:
        await callback.answer("Кнопка устарела. Начните ещё раз.", show_alert=True)
        return
    await callback.answer("Продолжаю…")
    await _choose_campaign(
        callback,
        state,
        data=data,
        campaign_id=campaign_id,
        campaign_name=campaign_name,
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


@router.callback_query(F.data.startswith("cpo:more:"))
async def open_more(callback: CallbackQuery) -> None:
    token = str(callback.data).split(":", 2)[2]
    await control._actor(int(callback.from_user.id), control._token_uuid(token))
    await control._callback_message(callback).answer(
        "⚙️ Ещё\n\nРедко используемые функции убраны сюда.",
        reply_markup=control._keyboard(
            [
                [("🧰 Услуги и расписание", f"cpo:work:{token}")],
                [("📣 Реклама и продвижение", f"cpo:ads:{token}")],
                [("🤝 Партнёрства", f"cpg:home:{token}")],
                [("⚙️ Настройки", f"cps:advanced:{token}")],
                [("🏠 В кабинет", f"cpj:home:{token}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cpo:work:"))
async def open_work_tools(callback: CallbackQuery) -> None:
    token = str(callback.data).split(":", 2)[2]
    await control._actor(int(callback.from_user.id), control._token_uuid(token))
    await control._callback_message(callback).answer(
        "🧰 Услуги и расписание",
        reply_markup=control._keyboard(
            [
                [("🧰 Мои услуги", f"cpj:services:{token}")],
                [("📅 Мой календарь", f"cpj:calendar:{token}:30")],
                [("🔗 Моя страница", f"cpj:page:{token}")],
                [("⬅️ Назад", f"cpo:more:{token}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cpo:ads:"))
async def open_ad_tools(callback: CallbackQuery) -> None:
    token = str(callback.data).split(":", 2)[2]
    await control._actor(int(callback.from_user.id), control._token_uuid(token))
    await control._callback_message(callback).answer(
        "📣 Реклама и продвижение\n\nОбычный путь — «Получить клиентов».",
        reply_markup=control._keyboard(
            [
                [("🚀 Получить клиентов", f"cpo:start:{token}")],
                [("📣 Яндекс Директ", f"cpa:home:{token}")],
                [("📊 Результаты Яндекс", f"cpy:a:{token}:30")],
                [("📣 Партнёрские материалы", f"cpg:materials:{token}")],
                [("⬅️ Назад", f"cpo:more:{token}")],
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
]

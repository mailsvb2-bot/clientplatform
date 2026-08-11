from __future__ import annotations

"""One-click owner UX over the canonical ClientPlatform application boundaries.

The module automates deterministic, reversible preparation only. It never
changes Yandex budgets or strategies, never submits moderation and never starts
ad spend. Paid visual generation remains an explicit owner action.
"""

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
from clientplatform.integrations.yandex_direct import YandexDirectError

from . import clientplatform_ad_connections as ad
from . import clientplatform_control as control
from . import clientplatform_simple_experience as simple

router = Router(name="clientplatform_one_click_experience")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

_COMMON_REGIONS = {
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


def _token(business_id: str) -> str:
    return control._uuid_token(business_id)


def _home_keyboard(business_id: str) -> InlineKeyboardMarkup:
    token = _token(business_id)
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
    _actor, access, _profile, _capabilities, _customers, _programs, slots = (
        await simple._business_snapshot(user_id=user_id, business_id=business_id)
    )
    open_slots = [item for item in slots if item.slot.status == BookingSlotStatus.OPEN]
    status = (
        f"Свободных времён для записи: {len(open_slots)}."
        if open_slots
        else "Свободного времени пока нет — я помогу открыть его по ходу."
    )
    await message.answer(
        f"🏠 {access.business.name}\n\n"
        "Здесь всё начинается с одной кнопки.\n\n"
        "Нажмите «🚀 Получить клиентов». Я сам проверю запись, рекламу и уже "
        "сохранённые настройки, пропущу ненужные шаги и спрошу только то, чего "
        "действительно не могу определить сам.\n\n"
        f"{status}",
        reply_markup=_home_keyboard(business_id),
    )


def _recent_job_for_connection(jobs, connection_id: str):
    return next(
        (item for item in jobs if str(getattr(item, "connection_id", "")) == connection_id),
        None,
    )


def _recent_job_for_campaign(jobs, connection_id: str, campaign_id: str):
    return next(
        (
            item
            for item in jobs
            if str(getattr(item, "connection_id", "")) == connection_id
            and str(getattr(item, "external_campaign_id", "")) == campaign_id
        ),
        None,
    )


def _eligible_campaigns(campaigns):
    return [
        item
        for item in campaigns
        if str(getattr(item, "state", "")) != "ARCHIVED"
        and str(getattr(item, "status", "")) == "ACCEPTED"
    ]


def _target(event: CallbackQuery | Message) -> Message:
    if isinstance(event, CallbackQuery):
        return control._callback_message(event)
    return event


def _event_user_id(event: CallbackQuery | Message) -> int:
    if event.from_user is None:
        raise ValueError("ClientPlatform requires a Telegram user")
    return int(event.from_user.id)


async def _bot_username(event: CallbackQuery | Message) -> str:
    bot = await event.bot.get_me()
    username = str(getattr(bot, "username", "") or "").strip()
    if not username:
        raise RuntimeError("ClientPlatform bot requires a public username")
    return username


def _promotion_link(username: str, source_token: str) -> str:
    return f"https://t.me/{username}?start={promotion_start_payload(source_token)}"


async def _load_open_slot(actor, slot_id: str):
    slots = await asyncio.to_thread(control.list_booking_slots, actor=actor)
    return next(
        (
            item
            for item in slots
            if str(item.slot.id) == str(slot_id)
            and item.slot.status == BookingSlotStatus.OPEN
        ),
        None,
    )


async def _send_share_fallback(
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
            "Свободное время уже изменилось. Нажмите «Получить клиентов» ещё раз — "
            "я заново проверю актуальное состояние.",
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
        username = await _bot_username(callback)
    except (PromotionError, RuntimeError, ValueError):
        await state.clear()
        await control._callback_message(callback).answer(
            f"{reason}\n\nНе удалось автоматически собрать запасной вариант. "
            "Откройте «Ещё → Реклама и продвижение» и повторите позже.",
            reply_markup=control._keyboard(
                [[("🏠 В кабинет", f"cpj:home:{business_token}")]]
            ),
        )
        return

    link = _promotion_link(username, view.campaign.source_token)
    creative = view.campaign.creative
    text = f"{creative.headline}\n\n{creative.primary_text}\n\n{creative.description}"
    share_url = "https://t.me/share/url?" + urlencode({"url": link, "text": text})
    await state.clear()
    await control._callback_message(callback).answer(
        "✅ Уже можно привлекать клиентов\n\n"
        f"{reason}\n\n"
        f"Я не оставил Вас в тупике: сам выбрал ближайшее свободное время "
        f"«{slot.offering_title}» — {slot.local_start} — и подготовил объявление "
        "с отдельной измеряемой ссылкой.\n\n"
        f"{text}\n\nЗаписаться: {link}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📨 Отправить объявление", url=share_url)],
                [
                    InlineKeyboardButton(
                        text="🔄 Проверить Яндекс ещё раз",
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


async def _open_yandex_oauth(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    actor,
    business_token: str,
) -> None:
    try:
        start = await asyncio.to_thread(start_yandex_direct_oauth, actor=actor)
    except (AdConnectionError, YandexDirectError, RuntimeError, ValueError):
        await state.clear()
        await control._callback_message(callback).answer(
            "Не удалось открыть подключение Яндекса. Попробуйте ещё раз позже.",
            reply_markup=control._keyboard(
                [[("🏠 В кабинет", f"cpj:home:{business_token}")]]
            ),
        )
        return
    await state.clear()
    await control._callback_message(callback).answer(
        "Чтобы ClientPlatform сам готовил рекламные черновики, один раз подключите "
        "Яндекс Директ. Пароль остаётся у Яндекса — ClientPlatform его не видит.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔐 Подключить Яндекс",
                        url=start.authorization_url,
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


async def _prepare_direct_draft(
    event: CallbackQuery | Message,
    state: FSMContext,
    *,
    business_id: str,
    business_token: str,
    slot_id: str,
    connection_id: str,
    campaign_id: str,
    campaign_name: str,
    region_ids: tuple[int, ...],
) -> None:
    actor = await control._actor(_event_user_id(event), business_id)
    try:
        promotion = await asyncio.to_thread(
            create_slot_promotion,
            actor=actor,
            slot_id=slot_id,
            channel=PromotionChannel.WEBSITE,
        )
        username = await _bot_username(event)
        source_url = _promotion_link(username, promotion.campaign.source_token)
        draft = await asyncio.to_thread(
            create_ad_publication_draft,
            actor=actor,
            promotion_campaign_id=promotion.campaign.id,
            connection_id=connection_id,
            external_campaign_id=campaign_id,
            external_campaign_name=campaign_name,
            region_ids=region_ids,
            source_url=source_url,
        )
    except (AdConnectionError, PromotionError, RuntimeError, TypeError, ValueError):
        await state.clear()
        await _target(event).answer(
            "Не удалось автоматически подготовить рекламный черновик. Ничего не "
            "запущено и деньги не списывались. Попробуйте ещё раз.",
            reply_markup=control._keyboard(
                [
                    [("🔄 Попробовать снова", f"cpo:start:{business_token}")],
                    [("🏠 В кабинет", f"cpj:home:{business_token}")],
                ]
            ),
        )
        return

    await state.set_state(ad.AdConnectionState.confirming_publication)
    await state.set_data(
        {
            "business_id": business_id,
            "business_token": business_token,
            "promotion_campaign_id": promotion.campaign.id,
            "source_url": source_url,
            "connection_id": connection_id,
            "external_campaign_id": campaign_id,
            "external_campaign_name": campaign_name,
            "job_id": draft.job.id,
            "creative_title": draft.job.title,
            "creative_body": draft.job.text,
            "creative_job_id": "",
        }
    )
    await _target(event).answer(
        "✅ Реклама подготовлена\n\n"
        f"Кампания: {draft.campaign_name}\n"
        f"Регион: {', '.join(str(item) for item in draft.job.region_ids)}\n"
        f"Заголовок: {draft.job.title}\n"
        f"Текст: {draft.job.text}\n\n"
        "Я сам использовал сохранённые настройки там, где это было безопасно. "
        "Ничего не запущено: показов, модерации и расходов нет.\n\n"
        "Картинка создаётся только по Вашему явному нажатию, потому что это платная "
        "операция провайдера.",
        reply_markup=control._keyboard(
            [
                [("🖼 Создать красивую картинку", "cpa:creative:image")],
                [(ad._CONFIRM_DRAFT_LABEL, "cpa:confirm")],
                [("✏️ Изменить вручную", f"cpa:promote:{business_token}")],
                [("🏠 В кабинет", f"cpj:home:{business_token}")],
            ]
        ),
    )


async def _ask_region(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    data: dict,
) -> None:
    await state.set_state(OneClickOwnerState.waiting_region)
    await state.set_data(data)
    await control._callback_message(callback).answer(
        "Нужна только география рекламы — её нельзя безопасно угадать.\n\n"
        "Если подходит один из вариантов, просто нажмите кнопку. Это запомнится в "
        "рекламном черновике, и в следующий раз ClientPlatform использует прежний регион сам.",
        reply_markup=control._keyboard(
            [
                [("Нижний Новгород", "cpo:region:47"), ("Москва", "cpo:region:213")],
                [("Санкт-Петербург", "cpo:region:2")],
                [("Другой регион", "cpo:region:other")],
                [("🏠 Отмена", f"cpj:home:{data['business_token']}")],
            ]
        ),
    )


async def _continue_campaign(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    data: dict,
    campaign_id: str,
    campaign_name: str,
) -> None:
    actor = await control._actor(int(callback.from_user.id), str(data["business_id"]))
    jobs = await asyncio.to_thread(list_ad_publications, actor=actor)
    recent = _recent_job_for_campaign(jobs, str(data["connection_id"]), campaign_id)
    remembered_regions = tuple(getattr(recent, "region_ids", ()) or ()) if recent else ()
    next_data = dict(data)
    next_data.update(
        external_campaign_id=campaign_id,
        external_campaign_name=campaign_name,
    )
    if remembered_regions:
        await _prepare_direct_draft(
            callback,
            state,
            business_id=str(data["business_id"]),
            business_token=str(data["business_token"]),
            slot_id=str(data["slot_id"]),
            connection_id=str(data["connection_id"]),
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            region_ids=remembered_regions,
        )
        return
    await _ask_region(callback, state, data=next_data)


async def _continue_connection(
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
    except (
        asyncio.TimeoutError,
        AdConnectionError,
        YandexDirectError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        slot = await _load_open_slot(actor, str(data["slot_id"]))
        await _send_share_fallback(
            callback,
            state,
            actor=actor,
            business_token=str(data["business_token"]),
            slot=slot,
            reason="Яндекс сейчас не ответил. Это не должно останавливать Вашу работу.",
        )
        return

    eligible = _eligible_campaigns(campaigns)
    if not eligible:
        slot = await _load_open_slot(actor, str(data["slot_id"]))
        reason = (
            "Подходящая кампания Яндекса пока не готова — например, она может "
            "ждать оплату или модерацию."
            if campaigns
            else "В Яндекс Директе пока нет готовой кампании."
        )
        await _send_share_fallback(
            callback,
            state,
            actor=actor,
            business_token=str(data["business_token"]),
            slot=slot,
            reason=reason,
        )
        return

    jobs = await asyncio.to_thread(list_ad_publications, actor=actor)
    recent = _recent_job_for_connection(jobs, connection_id)
    recent_campaign_id = str(getattr(recent, "external_campaign_id", "")) if recent else ""
    selected = next(
        (item for item in eligible if str(item.campaign_id) == recent_campaign_id),
        None,
    )
    next_data = dict(data)
    next_data["connection_id"] = connection_id
    next_data["campaigns"] = [
        {"id": str(item.campaign_id), "name": str(item.name)} for item in eligible
    ]
    if selected is not None:
        await _continue_campaign(
            callback,
            state,
            data=next_data,
            campaign_id=str(selected.campaign_id),
            campaign_name=str(selected.name),
        )
        return
    if len(eligible) == 1:
        selected = eligible[0]
        await _continue_campaign(
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
        "У Вас несколько готовых рекламных кампаний. Я не буду угадывать, куда "
        "записывать черновик. Выберите одну — дальше всё снова сделаю сам.",
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
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    await callback.answer("Готовлю всё сам…")
    await state.clear()

    connections, slots, jobs = await asyncio.gather(
        asyncio.to_thread(list_ad_connections, actor=actor),
        asyncio.to_thread(control.list_booking_slots, actor=actor),
        asyncio.to_thread(list_ad_publications, actor=actor),
    )
    open_slots = [item for item in slots if item.slot.status == BookingSlotStatus.OPEN]
    if not open_slots:
        await control._callback_message(callback).answer(
            "Чтобы привлекать людей, им нужно куда-то записываться. Свободного времени "
            "пока нет — нажмите одну кнопку, и я помогу открыть ближайшее окно.",
            reply_markup=control._keyboard(
                [
                    [("➕ Открыть время", f"cps:firstbook:{business_token}")],
                    [("🏠 В кабинет", f"cpj:home:{business_token}")],
                ]
            ),
        )
        return

    slot = min(open_slots, key=lambda item: str(item.slot.starts_at))
    data = {
        "business_id": business_id,
        "business_token": business_token,
        "slot_id": str(slot.slot.id),
    }

    if not ad_connections_enabled() or not yandex_direct_provider_configured():
        await _send_share_fallback(
            callback,
            state,
            actor=actor,
            business_token=business_token,
            slot=slot,
            reason="Яндекс-реклама пока не включена на платформе.",
        )
        return

    active = [item for item in connections if item.status == AdConnectionStatus.ACTIVE]
    if not active:
        await _open_yandex_oauth(
            callback,
            state,
            actor=actor,
            business_token=business_token,
        )
        return

    recent_connection_id = next(
        (
            str(job.connection_id)
            for job in jobs
            if any(str(item.id) == str(job.connection_id) for item in active)
        ),
        "",
    )
    selected_connection = next(
        (item for item in active if str(item.id) == recent_connection_id),
        None,
    )
    if selected_connection is not None:
        await _continue_connection(
            callback,
            state,
            data=data,
            connection_id=str(selected_connection.id),
        )
        return
    if len(active) == 1:
        await _continue_connection(
            callback,
            state,
            data=data,
            connection_id=str(active[0].id),
        )
        return

    await state.set_state(OneClickOwnerState.selecting_connection)
    await state.set_data(
        {
            **data,
            "connection_ids": [str(item.id) for item in active],
        }
    )
    await control._callback_message(callback).answer(
        "Подключено несколько рекламных кабинетов. Выберите нужный один раз — "
        "ClientPlatform запомнит выбор по последнему черновику.",
        reply_markup=control._keyboard(
            [
                [(f"Яндекс · {item.external_login}", f"cpo:connection:{index}")]
                for index, item in enumerate(active)
            ]
            + [[("🏠 Отмена", f"cpj:home:{business_token}")]],
        ),
    )


@router.callback_query(
    OneClickOwnerState.selecting_connection,
    F.data.startswith("cpo:connection:"),
)
async def choose_one_click_connection(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        index = int(str(callback.data).split(":", 2)[2])
        connection_id = list(data["connection_ids"])[index]
    except (IndexError, KeyError, TypeError, ValueError):
        await callback.answer("Кнопка устарела. Начните ещё раз.", show_alert=True)
        return
    await callback.answer("Продолжаю…")
    await _continue_connection(callback, state, data=data, connection_id=str(connection_id))


@router.callback_query(
    OneClickOwnerState.selecting_campaign,
    F.data.startswith("cpo:campaign:"),
)
async def choose_one_click_campaign(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        index = int(str(callback.data).split(":", 2)[2])
        selected = list(data["campaigns"])[index]
    except (IndexError, KeyError, TypeError, ValueError):
        await callback.answer("Кнопка устарела. Начните ещё раз.", show_alert=True)
        return
    await callback.answer("Продолжаю…")
    await _continue_campaign(
        callback,
        state,
        data=data,
        campaign_id=str(selected["id"]),
        campaign_name=str(selected["name"]),
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
            "Напишите город, если это Москва, Нижний Новгород или Санкт-Петербург. "
            "Для другого города можно указать ID региона Яндекс Директа."
        )
        return
    regions = _COMMON_REGIONS.get(raw)
    if not regions:
        await callback.answer("Регион не найден", show_alert=True)
        return
    await callback.answer("Готовлю черновик…")
    await _prepare_direct_draft(
        callback,
        state,
        business_id=str(data["business_id"]),
        business_token=str(data["business_token"]),
        slot_id=str(data["slot_id"]),
        connection_id=str(data["connection_id"]),
        campaign_id=str(data["external_campaign_id"]),
        campaign_name=str(data["external_campaign_name"]),
        region_ids=regions,
    )


@router.message(OneClickOwnerState.waiting_region)
async def receive_one_click_region(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    raw = " ".join(str(message.text or "").strip().lower().split())
    regions = _COMMON_REGIONS.get(raw)
    if regions is None:
        try:
            parsed = tuple(int(item.strip()) for item in raw.split(",") if item.strip())
            if not parsed or any(item <= 0 for item in parsed):
                raise ValueError
            regions = parsed
        except ValueError:
            await message.answer(
                "Не смог определить регион. Можно написать «Москва», «Нижний Новгород», "
                "«Санкт-Петербург» или ID региона из Яндекс Директа."
            )
            return
    await _prepare_direct_draft(
        message,
        state,
        business_id=str(data["business_id"]),
        business_token=str(data["business_token"]),
        slot_id=str(data["slot_id"]),
        connection_id=str(data["connection_id"]),
        campaign_id=str(data["external_campaign_id"]),
        campaign_name=str(data["external_campaign_name"]),
        region_ids=regions,
    )


@router.callback_query(F.data.startswith("cpo:more:"))
async def open_more(callback: CallbackQuery) -> None:
    token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(token)
    await control._actor(int(callback.from_user.id), business_id)
    await callback.answer()
    await control._callback_message(callback).answer(
        "⚙️ Ещё\n\nРедко используемые функции собраны здесь, чтобы не мешать основному пути.",
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
    business_id = control._token_uuid(token)
    await control._actor(int(callback.from_user.id), business_id)
    await callback.answer()
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
    business_id = control._token_uuid(token)
    await control._actor(int(callback.from_user.id), business_id)
    await callback.answer()
    await control._callback_message(callback).answer(
        "📣 Реклама и продвижение\n\nОбычный путь — «Получить клиентов». Здесь оставлены ручные инструменты.",
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

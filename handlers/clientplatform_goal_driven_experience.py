from __future__ import annotations

"""Goal-driven owner UX: ask for intent, hide internal advertising machinery."""

import asyncio
import hashlib
import os
import re
from types import ModuleType
from urllib.parse import urlencode

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from clientplatform.application.ad_connections import (
    ad_connections_enabled,
    confirm_ad_publication,
    create_ad_publication_draft,
    list_ad_connections,
    list_ad_publications,
    list_yandex_direct_campaigns,
    start_yandex_direct_oauth,
    yandex_direct_provider_configured,
)
from clientplatform.application.promotions import create_slot_promotion, promotion_start_payload
from clientplatform.application.visual_creatives import (
    VisualCreativeError,
    create_ad_visual,
    materialize_ad_visual,
    poll_ad_visual,
)
from clientplatform.domain.activity import ActivityError, CapabilityStatus
from clientplatform.domain.ad_connections import AdConnectionError, AdConnectionStatus
from clientplatform.domain.bookings import BookingError, BookingSlotStatus
from clientplatform.domain.promotions import PromotionChannel, PromotionError
from clientplatform.domain.tenancy import TenantPermissionDenied
from clientplatform.integrations.yandex_direct import YandexDirectError

from . import clientplatform_control as control
from . import clientplatform_one_click_experience as one_click
from . import clientplatform_simple_experience as simple

router = Router(name="clientplatform_goal_driven_experience")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

_REGIONS = {
    "47": (47,),
    "нижний новгород": (47,),
    "нижнем новгороде": (47,),
    "213": (213,),
    "москва": (213,),
    "москве": (213,),
    "2": (2,),
    "санкт-петербург": (2,),
    "санкт петербург": (2,),
    "санкт-петербурге": (2,),
    "спб": (2,),
}
_VISUAL_TASKS: set[asyncio.Task[None]] = set()


class GoalDrivenOwnerState(StatesGroup):
    waiting_offering_title = State()
    waiting_booking_start = State()
    waiting_booking_duration = State()


def _home_keyboard(business_id: str) -> InlineKeyboardMarkup:
    token = control._uuid_token(business_id)
    return control._keyboard(
        [
            [("🚀 Хочу клиентов", f"cpo:start:{token}")],
            [
                ("👥 Клиенты и запись", f"cpj:bookings:{token}"),
                ("⚙️ Управление", f"cpo:more:{token}"),
            ],
        ]
    )


async def send_goal_dashboard(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    _actor, access, _profile, _caps, _customers, _programs, slots = (
        await simple._business_snapshot(user_id=user_id, business_id=business_id)
    )
    open_count = sum(item.slot.status == BookingSlotStatus.OPEN for item in slots)
    availability = (
        f"Сейчас можно записать клиентов на {open_count} свободн. врем."
        if open_count
        else "Свободного времени пока нет — если понадобится, я сам попрошу его указать."
    )
    await message.answer(
        f"🏠 {access.business.name}\n\n"
        "Что хотите получить?\n\n"
        "Если нужны новые клиенты — нажмите одну кнопку. Я сам выберу ближайшее "
        "свободное время, подготовлю текст, проверю доступное продвижение и использую "
        "уже известные настройки. Технические параметры выбирать не придётся.\n\n"
        f"{availability}",
        reply_markup=_home_keyboard(business_id),
    )


def _target(event: CallbackQuery | Message) -> Message:
    return event if isinstance(event, Message) else control._callback_message(event)


def _user_id(event: CallbackQuery | Message) -> int:
    if event.from_user is None:
        raise ValueError("Telegram user is required")
    return int(event.from_user.id)


async def _bot_username(event: CallbackQuery | Message) -> str:
    username = str(getattr(await event.bot.get_me(), "username", "") or "").strip()
    if not username:
        raise RuntimeError("ClientPlatform bot requires a public username")
    return username


def _promotion_link(username: str, source_token: str) -> str:
    return f"https://t.me/{username}?start={promotion_start_payload(source_token)}"


def _share_url(url: str, text: str) -> str:
    return "https://t.me/share/url?" + urlencode({"url": url, "text": text})


def _ordered_jobs(jobs):
    return sorted(
        jobs,
        key=lambda item: str(getattr(item, "updated_at", "") or getattr(item, "created_at", "")),
        reverse=True,
    )


def _pick_connection(active, jobs):
    active_by_id = {str(item.id): item for item in active}
    for job in _ordered_jobs(jobs):
        selected = active_by_id.get(str(getattr(job, "connection_id", "")))
        if selected is not None:
            return selected
    return active[0] if len(active) == 1 else None


def _eligible_campaigns(campaigns):
    return [
        item
        for item in campaigns
        if str(getattr(item, "state", "")).strip().upper() == "ON"
        and str(getattr(item, "status", "")).strip().upper() == "ACCEPTED"
    ]


def _pick_campaign(campaigns, jobs, *, connection_id: str):
    available = {str(item.campaign_id): item for item in campaigns}
    for job in _ordered_jobs(jobs):
        if str(getattr(job, "connection_id", "")) != connection_id:
            continue
        selected = available.get(str(getattr(job, "external_campaign_id", "")))
        if selected is not None:
            return selected
    return campaigns[0] if len(campaigns) == 1 else None


def _pick_regions(jobs, *, connection_id: str, campaign_id: str) -> tuple[int, ...]:
    ordered = _ordered_jobs(jobs)
    for job in ordered:
        if (
            str(getattr(job, "connection_id", "")) == connection_id
            and str(getattr(job, "external_campaign_id", "")) == campaign_id
        ):
            regions = tuple(getattr(job, "region_ids", ()) or ())
            if regions:
                return regions
    for job in ordered:
        if str(getattr(job, "connection_id", "")) == connection_id:
            regions = tuple(getattr(job, "region_ids", ()) or ())
            if regions:
                return regions
    return ()


async def _load_open_slot(actor, slot_id: str):
    slots = await asyncio.to_thread(control.list_booking_slots, actor=actor)
    return next(
        (
            item
            for item in slots
            if str(item.slot.id) == slot_id and item.slot.status == BookingSlotStatus.OPEN
        ),
        None,
    )


async def _share_result(
    event: CallbackQuery | Message,
    state: FSMContext,
    *,
    actor,
    business_token: str,
    slot,
    note: str,
    enable_paid_url: str = "",
    show_paid_settings: bool = False,
) -> None:
    await state.clear()
    target = _target(event)
    if slot is None:
        await target.answer(
            "Свободное время изменилось. Нажмите «🚀 Хочу клиентов» ещё раз — я всё "
            "пересоберу автоматически.",
            reply_markup=control._keyboard(
                [[("🚀 Хочу клиентов", f"cpo:start:{business_token}")]]
            ),
        )
        return
    try:
        promotion = await asyncio.to_thread(
            create_slot_promotion,
            actor=actor,
            slot_id=slot.slot.id,
            channel=PromotionChannel.TELEGRAM,
        )
        link = _promotion_link(
            await _bot_username(event),
            promotion.campaign.source_token,
        )
    except (PromotionError, TenantPermissionDenied, RuntimeError, ValueError):
        await target.answer(
            "Не получилось автоматически собрать объявление. Ничего не запущено и "
            "деньги не списывались.",
            reply_markup=control._keyboard(
                [[("🚀 Попробовать ещё раз", f"cpo:start:{business_token}")]]
            ),
        )
        return
    creative = promotion.campaign.creative
    text = f"{creative.headline}\n\n{creative.primary_text}\n\n{creative.description}"
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="📨 Отправить людям", url=_share_url(link, text))]
    ]
    if enable_paid_url:
        rows.append(
            [InlineKeyboardButton(text="⚡ Включить платное продвижение", url=enable_paid_url)]
        )
    elif show_paid_settings:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⚙️ Настроить платное продвижение",
                    callback_data=f"cpo:ads:{business_token}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🏠 Готово",
                callback_data=f"cpj:home:{business_token}",
            )
        ]
    )
    await target.answer(
        "✅ Уже можно получать клиентов\n\n"
        f"Я сам выбрал ближайшее свободное время: {slot.local_start} · "
        f"{slot.offering_title}.\n{note}\n\n"
        f"{text}\n\nЗаписаться: {link}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _oauth_url(actor) -> str:
    try:
        start = await asyncio.to_thread(start_yandex_direct_oauth, actor=actor)
    except (AdConnectionError, YandexDirectError, TenantPermissionDenied, RuntimeError, ValueError):
        return ""
    return start.authorization_url


async def _ask_booking_start(
    target: Message,
    state: FSMContext,
    *,
    business_id: str,
    business_token: str,
    offering,
) -> None:
    await state.set_state(GoalDrivenOwnerState.waiting_booking_start)
    await state.set_data(
        {
            "business_id": business_id,
            "business_token": business_token,
            "offering_id": str(offering.id),
            "offering_title": str(offering.title),
        }
    )
    await target.answer(
        f"Когда Вы можете принять нового клиента на «{offering.title}»?\n\n"
        "Напишите дату и время, например: 20.08 12:00. Больше ничего настраивать не нужно."
    )


async def _begin_missing_schedule(
    event: CallbackQuery | Message,
    state: FSMContext,
    *,
    actor,
    business_id: str,
    business_token: str,
) -> None:
    target = _target(event)
    capabilities = await asyncio.to_thread(control.list_business_capabilities, actor=actor)
    usable = [
        item
        for item in capabilities
        if item.status == CapabilityStatus.ACTIVE
        and item.connector_key in {"consultations", "services", "custom"}
    ]
    if not usable:
        try:
            usable = [
                await asyncio.to_thread(
                    control.enable_business_capability,
                    actor=actor,
                    connector_key="services",
                )
            ]
        except (ActivityError, TenantPermissionDenied, ValueError):
            await target.answer(
                "Мне не удалось автоматически подготовить запись. Ничего опасного не "
                "изменено.",
                reply_markup=control._keyboard(
                    [[("🏠 В кабинет", f"cpj:home:{business_token}")]]
                ),
            )
            return
    groups = await asyncio.gather(
        *[
            asyncio.to_thread(
                control.list_business_offerings,
                actor=actor,
                capability_id=capability.id,
            )
            for capability in usable
        ]
    )
    offerings = [item for group in groups for item in group]
    if not offerings:
        capability = usable[0]
        await state.set_state(GoalDrivenOwnerState.waiting_offering_title)
        await state.set_data(
            {
                "business_id": business_id,
                "business_token": business_token,
                "capability_id": str(capability.id),
            }
        )
        await target.answer(
            "Как называется то, на что Вы хотите получить клиента?\n\n"
            "Например: «Консультация», «Ремонт раковины» или «Занятие английским»."
        )
        return
    if len(offerings) == 1:
        await _ask_booking_start(
            target,
            state,
            business_id=business_id,
            business_token=business_token,
            offering=offerings[0],
        )
        return
    await state.clear()
    rows = [
        [
            (
                f"🎯 {offering.title[:42]}",
                f"cpo:offer:{business_token}:{control._uuid_token(offering.id)}",
            )
        ]
        for offering in offerings[:12]
    ]
    rows.append([("🏠 Отмена", f"cpj:home:{business_token}")])
    await target.answer(
        "Для чего сейчас нужен новый клиент?",
        reply_markup=control._keyboard(rows),
    )


async def _find_offering(actor, offering_id: str):
    capabilities = await asyncio.to_thread(control.list_business_capabilities, actor=actor)
    groups = await asyncio.gather(
        *[
            asyncio.to_thread(
                control.list_business_offerings,
                actor=actor,
                capability_id=capability.id,
            )
            for capability in capabilities
            if capability.status == CapabilityStatus.ACTIVE
        ]
    )
    return next(
        (item for group in groups for item in group if str(item.id) == offering_id),
        None,
    )


def _duration_from_title(title: str) -> int | None:
    match = re.search(r"\b([1-9][0-9]{0,2})\s*(?:мин|минут)", str(title).lower())
    if match is None:
        return None
    value = int(match.group(1))
    return value if 5 <= value <= 720 else None


async def _create_slot_and_continue(
    message: Message,
    state: FSMContext,
    *,
    data: dict,
    duration: int,
) -> None:
    business_id = str(data["business_id"])
    business_token = str(data["business_token"])
    actor = await control._actor(_user_id(message), business_id)
    try:
        slot = await asyncio.to_thread(
            control.create_booking_slot,
            actor=actor,
            offering_id=str(data["offering_id"]),
            local_start=str(data["booking_start"]),
            duration_minutes=duration,
        )
    except (BookingError, ValueError, TypeError):
        await message.answer(
            "Такое время не получилось сохранить. Напишите дату и время ещё раз, "
            "например: 20.08 12:00."
        )
        await state.set_state(GoalDrivenOwnerState.waiting_booking_start)
        return
    await state.clear()
    await message.answer("✅ Время добавил. Теперь сам готовлю всё для привлечения клиентов…")
    await _continue_goal(
        message,
        state,
        actor=actor,
        business_id=business_id,
        business_token=business_token,
        slot=slot,
    )


async def _ask_city(
    event: CallbackQuery | Message,
    state: FSMContext,
    *,
    data: dict,
) -> None:
    await state.set_state(one_click.OneClickOwnerState.waiting_region)
    await state.set_data(data)
    await _target(event).answer(
        "Где Вы хотите находить новых клиентов?\n\n"
        "Я запомню выбор и дальше буду использовать его сам.",
        reply_markup=control._keyboard(
            [
                [("Нижний Новгород", "cpo:region:47"), ("Москва", "cpo:region:213")],
                [("Санкт-Петербург", "cpo:region:2")],
                [("Другой город", "cpo:region:other")],
            ]
        ),
    )


async def _prepare_and_queue(
    event: CallbackQuery | Message,
    state: FSMContext,
    *,
    actor,
    business_id: str,
    business_token: str,
    slot,
    connection_id: str,
    campaign_id: str,
    campaign_name: str,
    region_ids: tuple[int, ...],
) -> None:
    target = _target(event)
    try:
        promotion = await asyncio.to_thread(
            create_slot_promotion,
            actor=actor,
            slot_id=slot.slot.id,
            channel=PromotionChannel.WEBSITE,
        )
        source_url = _promotion_link(
            await _bot_username(event),
            promotion.campaign.source_token,
        )
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
        queued = await asyncio.to_thread(
            confirm_ad_publication,
            actor=actor,
            job_id=draft.job.id,
        )
    except (
        AdConnectionError,
        PromotionError,
        TenantPermissionDenied,
        RuntimeError,
        ValueError,
        TypeError,
    ):
        await _share_result(
            event,
            state,
            actor=actor,
            business_token=business_token,
            slot=slot,
            note="Платное продвижение сейчас не удалось подготовить, поэтому я не остановил работу и сделал безопасный вариант без расходов.",
            show_paid_settings=True,
        )
        return
    await state.clear()
    creative = promotion.campaign.creative
    text = f"{creative.headline}\n\n{creative.primary_text}\n\n{creative.description}"
    share = _share_url(source_url, text)
    visual_callback = (
        f"cpo:visual:{business_token}:{control._uuid_token(queued.id)}"
    )
    await target.answer(
        "✅ Всё подготовил\n\n"
        f"Выбрал ближайшее свободное время: {slot.local_start} · {slot.offering_title}.\n\n"
        f"{draft.job.title}\n{draft.job.text}\n\n"
        "Безопасный рекламный черновик уже готовится автоматически. Показы не "
        "запущены, бюджет не менялся и деньги не расходуются.\n\n"
        "Если хотите красивую картинку, это отдельная платная генерация — она "
        "запустится только после явного нажатия кнопки ниже.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✨ Добавить красивую картинку · платно",
                        callback_data=visual_callback,
                    )
                ],
                [InlineKeyboardButton(text="📨 Отправить людям", url=share)],
                [
                    InlineKeyboardButton(
                        text="🏠 Готово",
                        callback_data=f"cpj:home:{business_token}",
                    )
                ],
            ]
        ),
    )


async def _continue_goal(
    event: CallbackQuery | Message,
    state: FSMContext,
    *,
    actor,
    business_id: str,
    business_token: str,
    slot,
) -> None:
    if not ad_connections_enabled() or not yandex_direct_provider_configured():
        await _share_result(
            event,
            state,
            actor=actor,
            business_token=business_token,
            slot=slot,
            note="Платное продвижение на платформе пока не включено, поэтому я сразу подготовил вариант без расходов.",
        )
        return
    try:
        connections, jobs = await asyncio.gather(
            asyncio.to_thread(list_ad_connections, actor=actor),
            asyncio.to_thread(list_ad_publications, actor=actor),
        )
    except TenantPermissionDenied:
        await _share_result(
            event,
            state,
            actor=actor,
            business_token=business_token,
            slot=slot,
            note="Для Вашей роли платное продвижение недоступно, но готовое объявление уже можно отправлять людям.",
        )
        return
    except AdConnectionError:
        await _share_result(
            event,
            state,
            actor=actor,
            business_token=business_token,
            slot=slot,
            note="Платное продвижение временно недоступно, поэтому я продолжил без него.",
        )
        return
    active = [item for item in connections if item.status == AdConnectionStatus.ACTIVE]
    if not active:
        await _share_result(
            event,
            state,
            actor=actor,
            business_token=business_token,
            slot=slot,
            note="Бесплатный вариант уже готов. Если захотите платное продвижение, разрешение можно дать один раз — дальше я буду использовать его сам.",
            enable_paid_url=await _oauth_url(actor),
        )
        return
    connection = _pick_connection(active, jobs)
    if connection is None:
        await _share_result(
            event,
            state,
            actor=actor,
            business_token=business_token,
            slot=slot,
            note="У платного продвижения есть несколько возможных настроек. Я не стал угадывать и рисковать — безопасный вариант уже готов.",
            show_paid_settings=True,
        )
        return
    try:
        campaigns = await asyncio.wait_for(
            asyncio.to_thread(
                list_yandex_direct_campaigns,
                actor=actor,
                connection_id=str(connection.id),
            ),
            timeout=25.0,
        )
    except (asyncio.TimeoutError, AdConnectionError, YandexDirectError, TenantPermissionDenied):
        await _share_result(
            event,
            state,
            actor=actor,
            business_token=business_token,
            slot=slot,
            note="Платное продвижение пока не готово, поэтому я сразу подготовил вариант без расходов.",
        )
        return
    eligible = _eligible_campaigns(campaigns)
    if not eligible:
        await _share_result(
            event,
            state,
            actor=actor,
            business_token=business_token,
            slot=slot,
            note="Платное продвижение пока не готово к работе. Я не стал заставлять Вас разбираться с этим и уже сделал вариант без расходов.",
        )
        return
    campaign = _pick_campaign(eligible, jobs, connection_id=str(connection.id))
    if campaign is None:
        await _share_result(
            event,
            state,
            actor=actor,
            business_token=business_token,
            slot=slot,
            note="Есть несколько вариантов платного продвижения, и я не буду выбирать наугад. Безопасный вариант уже готов.",
            show_paid_settings=True,
        )
        return
    regions = _pick_regions(
        jobs,
        connection_id=str(connection.id),
        campaign_id=str(campaign.campaign_id),
    )
    data = {
        "business_id": business_id,
        "business_token": business_token,
        "slot_id": str(slot.slot.id),
        "connection_id": str(connection.id),
        "external_campaign_id": str(campaign.campaign_id),
        "external_campaign_name": str(campaign.name),
    }
    if not regions:
        await _ask_city(event, state, data=data)
        return
    await _prepare_and_queue(
        event,
        state,
        actor=actor,
        business_id=business_id,
        business_token=business_token,
        slot=slot,
        connection_id=str(connection.id),
        campaign_id=str(campaign.campaign_id),
        campaign_name=str(campaign.name),
        region_ids=regions,
    )


@router.callback_query(F.data.startswith("cpo:start:"))
async def get_clients_goal(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    await callback.answer("Делаю всё сам…")
    await state.clear()
    slots = await asyncio.to_thread(control.list_booking_slots, actor=actor)
    open_slots = [item for item in slots if item.slot.status == BookingSlotStatus.OPEN]
    if not open_slots:
        await _begin_missing_schedule(
            callback,
            state,
            actor=actor,
            business_id=business_id,
            business_token=business_token,
        )
        return
    slot = min(open_slots, key=lambda item: item.slot.starts_at)
    await _continue_goal(
        callback,
        state,
        actor=actor,
        business_id=business_id,
        business_token=business_token,
        slot=slot,
    )


@router.callback_query(F.data.startswith("cpo:offer:"))
async def choose_goal_offering(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, business_token, offering_token = str(callback.data).split(":", 3)
        business_id = control._token_uuid(business_token)
        offering_id = control._token_uuid(offering_token)
        actor = await control._actor(int(callback.from_user.id), business_id)
        offering = await _find_offering(actor, offering_id)
    except (ValueError, TenantPermissionDenied):
        offering = None
    if offering is None:
        await callback.answer("Не получилось выбрать услугу. Начните ещё раз.", show_alert=True)
        return
    await callback.answer()
    await _ask_booking_start(
        control._callback_message(callback),
        state,
        business_id=business_id,
        business_token=business_token,
        offering=offering,
    )


@router.message(GoalDrivenOwnerState.waiting_offering_title)
async def receive_goal_offering_title(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    title = " ".join(str(message.text or "").split()).strip()
    if not title:
        await message.answer("Напишите короткое название, например: «Консультация».")
        return
    try:
        business_id = str(data["business_id"])
        business_token = str(data["business_token"])
        actor = await control._actor(_user_id(message), business_id)
        offering = await asyncio.to_thread(
            control.create_business_offering,
            actor=actor,
            capability_id=str(data["capability_id"]),
            title=title,
            description=f"{title}. Запись через ClientPlatform.",
        )
    except (KeyError, ActivityError, TenantPermissionDenied, ValueError):
        await message.answer("Не получилось сохранить название. Попробуйте написать его ещё раз.")
        return
    await _ask_booking_start(
        message,
        state,
        business_id=business_id,
        business_token=business_token,
        offering=offering,
    )


@router.message(GoalDrivenOwnerState.waiting_booking_start)
async def receive_goal_booking_start(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    value = " ".join(str(message.text or "").split()).strip()
    if not value:
        await message.answer("Напишите дату и время, например: 20.08 12:00.")
        return
    await state.set_data({**data, "booking_start": value})
    inferred = _duration_from_title(str(data.get("offering_title") or ""))
    if inferred is not None:
        await _create_slot_and_continue(message, state, data={**data, "booking_start": value}, duration=inferred)
        return
    await state.set_state(GoalDrivenOwnerState.waiting_booking_duration)
    await message.answer("Сколько минут обычно занимает эта встреча или услуга? Например: 60")


@router.message(GoalDrivenOwnerState.waiting_booking_duration)
async def receive_goal_booking_duration(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        duration = int(str(message.text or "").strip())
    except ValueError:
        duration = 0
    if not 5 <= duration <= 720:
        await message.answer("Напишите только число минут, например: 60.")
        return
    await _create_slot_and_continue(message, state, data=data, duration=duration)


@router.callback_query(
    one_click.OneClickOwnerState.waiting_region,
    F.data.startswith("cpo:region:"),
)
async def choose_goal_region(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    raw = str(callback.data).split(":", 2)[2]
    if raw == "other":
        await callback.answer()
        await control._callback_message(callback).answer("Напишите город обычными словами.")
        return
    regions = _REGIONS.get(raw)
    if regions is None:
        await callback.answer("Не получилось определить город", show_alert=True)
        return
    actor = await control._actor(int(callback.from_user.id), str(data["business_id"]))
    slot = await _load_open_slot(actor, str(data["slot_id"]))
    if slot is None:
        await state.clear()
        await control._callback_message(callback).answer(
            "Свободное время уже изменилось. Нажмите «🚀 Хочу клиентов» ещё раз."
        )
        return
    await callback.answer("Продолжаю…")
    await _prepare_and_queue(
        callback,
        state,
        actor=actor,
        business_id=str(data["business_id"]),
        business_token=str(data["business_token"]),
        slot=slot,
        connection_id=str(data["connection_id"]),
        campaign_id=str(data["external_campaign_id"]),
        campaign_name=str(data["external_campaign_name"]),
        region_ids=regions,
    )


@router.message(one_click.OneClickOwnerState.waiting_region)
async def receive_goal_region(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    raw = " ".join(str(message.text or "").strip().lower().split())
    regions = _REGIONS.get(raw)
    actor = await control._actor(_user_id(message), str(data["business_id"]))
    slot = await _load_open_slot(actor, str(data["slot_id"]))
    if regions is None:
        await _share_result(
            message,
            state,
            actor=actor,
            business_token=str(data["business_token"]),
            slot=slot,
            note="Этот город я пока не могу определить без риска ошибиться. Поэтому уже подготовил безопасный вариант без расходов.",
            show_paid_settings=True,
        )
        return
    if slot is None:
        await state.clear()
        await message.answer("Свободное время уже изменилось. Нажмите «🚀 Хочу клиентов» ещё раз.")
        return
    await _prepare_and_queue(
        message,
        state,
        actor=actor,
        business_id=str(data["business_id"]),
        business_token=str(data["business_token"]),
        slot=slot,
        connection_id=str(data["connection_id"]),
        campaign_id=str(data["external_campaign_id"]),
        campaign_name=str(data["external_campaign_name"]),
        region_ids=regions,
    )


async def _send_visual(target: Message, job) -> bool:
    try:
        path = await asyncio.to_thread(materialize_ad_visual, job)
    except VisualCreativeError:
        return False
    await target.answer_photo(FSInputFile(path), caption="✨ Картинка готова")
    await target.answer(
        "Готово. Я ничего не запускал в рекламе и не менял бюджет."
    )
    return True


async def _finish_visual(target: Message, *, scope_id: str, job_id: str) -> None:
    for _ in range(12):
        await asyncio.sleep(5)
        try:
            job = await asyncio.to_thread(
                poll_ad_visual,
                job_id=job_id,
                scope_id=scope_id,
            )
        except VisualCreativeError:
            await target.answer("Картинку сейчас получить не удалось. Объявление без неё уже готово.")
            return
        if job.status == "succeeded" and job.asset_ready:
            if not await _send_visual(target, job):
                await target.answer("Картинка создана, но файл получить не удалось. Объявление без неё уже готово.")
            return
        if job.status == "failed":
            await target.answer("Картинка не создалась. Объявление без неё уже готово.")
            return
    await target.answer(
        "Генерация картинки заняла слишком много времени. Объявление без неё уже готово; "
        "эту же кнопку можно нажать позже — повторная попытка защищена от дублирования."
    )


def _track_task(task: asyncio.Task[None]) -> None:
    _VISUAL_TASKS.add(task)
    task.add_done_callback(_VISUAL_TASKS.discard)


@router.callback_query(F.data.startswith("cpo:visual:"))
async def generate_goal_visual(callback: CallbackQuery) -> None:
    try:
        _, _, business_token, job_token = str(callback.data).split(":", 3)
        business_id = control._token_uuid(business_token)
        job_id = control._token_uuid(job_token)
        actor = await control._actor(int(callback.from_user.id), business_id)
        jobs = await asyncio.to_thread(list_ad_publications, actor=actor)
        publication = next((item for item in jobs if str(item.id) == job_id), None)
    except (ValueError, TenantPermissionDenied, AdConnectionError):
        publication = None
    if publication is None:
        await callback.answer("Объявление больше не найдено", show_alert=True)
        return
    await callback.answer("Создаю картинку…")
    try:
        visual = await asyncio.to_thread(
            create_ad_visual,
            title=str(publication.title),
            body=str(publication.text),
            kind="image",
            scope_id=business_id,
            idempotency_key="clientplatform:" + hashlib.sha256(
                f"{business_id}|{job_id}|image".encode("utf-8")
            ).hexdigest(),
            country_code=os.getenv("VISUAL_DEPLOYMENT_COUNTRY", ""),
            wait_seconds=60,
        )
    except (VisualCreativeError, ValueError, TypeError):
        await control._callback_message(callback).answer(
            "Картинку сейчас создать не удалось. Объявление без неё уже готово."
        )
        return
    target = control._callback_message(callback)
    if visual.status == "succeeded" and visual.asset_ready:
        if not await _send_visual(target, visual):
            await target.answer("Картинка создана, но файл получить не удалось. Объявление без неё уже готово.")
        return
    if visual.status in {"queued", "running"}:
        await target.answer("Картинка создаётся. Ничего больше нажимать не нужно — пришлю её сюда автоматически.")
        _track_task(
            asyncio.create_task(
                _finish_visual(target, scope_id=business_id, job_id=visual.id)
            )
        )
        return
    await target.answer("Картинка не создалась. Объявление без неё уже готово.")


def install_goal_driven_experience(
    *,
    owner_module: ModuleType,
    simple_module: ModuleType,
    control_module: ModuleType,
    one_click_module: ModuleType,
) -> None:
    if bool(getattr(owner_module, "_goal_driven_experience_installed", False)):
        return
    one_click_module._home_keyboard = _home_keyboard
    owner_module._owner_keyboard = _home_keyboard
    owner_module.send_owner_dashboard = send_goal_dashboard
    simple_module.send_simple_dashboard = send_goal_dashboard
    control_module._send_dashboard = send_goal_dashboard
    owner_module._goal_driven_experience_installed = True


__all__ = [
    "GoalDrivenOwnerState",
    "generate_goal_visual",
    "get_clients_goal",
    "install_goal_driven_experience",
    "router",
    "send_goal_dashboard",
]

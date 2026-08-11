from __future__ import annotations

"""Goal-first owner UX layered over the canonical one-click orchestration.

The owner states the outcome (get clients). Technical routing stays inside the
platform. Paid or irreversible steps remain explicit confirmations.
"""

import asyncio
from types import ModuleType

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from clientplatform.domain.ad_connections import AdConnectionError
from clientplatform.domain.promotions import PromotionChannel, PromotionError
from clientplatform.domain.tenancy import TenantPermissionDenied

from . import clientplatform_ad_connections as ad
from . import clientplatform_control as control
from . import clientplatform_one_click_experience as one_click


def _goal_keyboard(business_id: str):
    token = control._uuid_token(business_id)
    return control._keyboard(
        [
            [("🚀 Хочу клиентов", f"cpo:start:{token}")],
            [
                ("👥 Записи", f"cpj:bookings:{token}"),
                ("⚙️ Настройки", f"cpo:more:{token}"),
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
        await one_click.simple._business_snapshot(
            user_id=user_id,
            business_id=business_id,
        )
    )
    open_count = sum(
        item.slot.status == one_click.BookingSlotStatus.OPEN for item in slots
    )
    readiness = (
        f"Свободных окон сейчас: {open_count}."
        if open_count
        else "Свободных окон пока нет — если понадобится, я попрошу указать одно."
    )
    await message.answer(
        f"🏠 {access.business.name}\n\n"
        "Что хотите получить?\n\n"
        "Нажмите «🚀 Хочу клиентов». ClientPlatform сама проверит запись, "
        "рекламу и прежние настройки, подготовит лучший доступный вариант и "
        "спросит только то, что действительно нельзя определить автоматически.\n\n"
        "Технические кабинеты, кампании и служебные настройки знать не нужно. "
        "Действие с возможными расходами всегда подтверждается отдельно.\n\n"
        f"{readiness}",
        reply_markup=_goal_keyboard(business_id),
    )


async def _prepare_goal_result(
    event: CallbackQuery | Message,
    state: FSMContext,
    *,
    data: dict,
    region_ids: tuple[int, ...],
) -> None:
    """Prepare the canonical ad draft but present only the business outcome."""

    actor = await control._actor(
        one_click._user_id(event),
        str(data["business_id"]),
    )
    try:
        promotion = await asyncio.to_thread(
            one_click.create_slot_promotion,
            actor=actor,
            slot_id=str(data["slot_id"]),
            channel=PromotionChannel.WEBSITE,
        )
    except (PromotionError, TenantPermissionDenied):
        await one_click._draft_failure(
            event,
            state,
            business_token=str(data["business_token"]),
        )
        return

    try:
        source_url = one_click._link(
            await one_click._username(event),
            promotion.campaign.source_token,
        )
    except (RuntimeError, ValueError):
        await one_click._draft_failure(
            event,
            state,
            business_token=str(data["business_token"]),
        )
        return

    try:
        draft = await asyncio.to_thread(
            one_click.create_ad_publication_draft,
            actor=actor,
            promotion_campaign_id=promotion.campaign.id,
            connection_id=str(data["connection_id"]),
            external_campaign_id=str(data["external_campaign_id"]),
            external_campaign_name=str(data["external_campaign_name"]),
            region_ids=region_ids,
            source_url=source_url,
        )
    except (AdConnectionError, TenantPermissionDenied):
        await one_click._draft_failure(
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

    await one_click._target(event).answer(
        "✅ Всё готово\n\n"
        "Я сам выбрал ближайшее свободное время, подготовил объявление и "
        "использовал подходящие сохранённые настройки.\n\n"
        f"{draft.job.title}\n\n{draft.job.text}\n\n"
        "Пока ничего не запущено и рекламный бюджет не расходуется. "
        "Теперь нужен только ваш выбор перед платным действием.",
        reply_markup=control._keyboard(
            [
                [("✨ Добавить красивую картинку", "cpa:creative:image")],
                [("🚀 Запустить рекламу", "cpa:confirm")],
                [("✏️ Изменить", f"cpa:promote:{data['business_token']}")],
                [("🏠 Не запускать", f"cpj:home:{data['business_token']}")],
            ]
        ),
    )


async def _choose_goal_region(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    data: dict,
    campaign_id: str,
    campaign_name: str,
) -> None:
    """Reuse a previous region; otherwise ask one plain business question."""

    actor = await control._actor(
        int(callback.from_user.id),
        str(data["business_id"]),
    )
    try:
        jobs = await asyncio.to_thread(one_click.list_ad_publications, actor=actor)
    except (AdConnectionError, TenantPermissionDenied):
        jobs = []

    saved = one_click._recent(
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
        await _prepare_goal_result(
            callback,
            state,
            data=next_data,
            region_ids=regions,
        )
        return

    await state.set_state(one_click.OneClickOwnerState.waiting_region)
    await state.set_data(next_data)
    await control._callback_message(callback).answer(
        "Где искать клиентов? Это нужно спросить только в первый раз — "
        "дальше ClientPlatform запомнит выбор.",
        reply_markup=control._keyboard(
            [
                [("Нижний Новгород", "cpo:region:47"), ("Москва", "cpo:region:213")],
                [("Санкт-Петербург", "cpo:region:2")],
                [("Другой город", "cpo:region:other")],
                [("🏠 Не сейчас", f"cpj:home:{data['business_token']}")],
            ]
        ),
    )


def install_goal_first_autopilot(
    *,
    owner_module: ModuleType,
    simple_module: ModuleType,
    control_module: ModuleType,
) -> None:
    if bool(getattr(owner_module, "_goal_first_autopilot_installed", False)):
        return

    # Keep the proven orchestration and safety boundaries, replace only the
    # owner-facing language and decision surface.
    one_click._home_keyboard = _goal_keyboard
    one_click._prepare_draft = _prepare_goal_result
    one_click._choose_campaign = _choose_goal_region

    owner_module._owner_keyboard = _goal_keyboard
    owner_module.send_owner_dashboard = send_goal_dashboard
    simple_module.send_simple_dashboard = send_goal_dashboard
    control_module._send_dashboard = send_goal_dashboard
    owner_module._goal_first_autopilot_installed = True


__all__ = [
    "install_goal_first_autopilot",
    "send_goal_dashboard",
]

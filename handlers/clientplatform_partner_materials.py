from __future__ import annotations

import asyncio

from aiogram import F
from aiogram.types import CallbackQuery

from clientplatform.application.partner_runtime import (
    list_partner_campaigns,
    list_partner_candidates,
)

from . import clientplatform_control as control
from . import clientplatform_simple_experience as simple


@simple.router.callback_query(F.data.startswith("cpg:materials:"))
async def open_partner_materials(callback: CallbackQuery) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    actor = await control._actor(
        int(callback.from_user.id),
        control._token_uuid(business_token),
    )
    campaigns = await asyncio.to_thread(list_partner_campaigns, actor=actor)
    rows = [
        [
            (
                f"📂 {campaign.name[:30]}",
                f"cpg:mc:{business_token}:{control._uuid_token(campaign.id)}",
            )
        ]
        for campaign in campaigns[:10]
    ]
    rows.extend(
        [
            [("🤝 К партнёрствам", f"cpg:home:{business_token}")],
            [("🏠 В кабинет", f"cpj:home:{business_token}")],
        ]
    )
    await callback.answer()
    await control._callback_message(callback).answer(
        "📣 Партнёрские материалы\n\n"
        "Здесь лежат готовые посты с отдельными referral-ссылками. "
        "Переход и подтверждённый результат считаются раздельно.",
        reply_markup=control._keyboard(rows),
    )


@simple.router.callback_query(F.data.startswith("cpg:mc:"))
async def open_partner_campaign_materials(callback: CallbackQuery) -> None:
    _, _, business_token, campaign_token = str(callback.data).split(":", 3)
    actor = await control._actor(
        int(callback.from_user.id),
        control._token_uuid(business_token),
    )
    candidates = await asyncio.to_thread(
        list_partner_candidates,
        actor=actor,
        campaign_id=control._token_uuid(campaign_token),
        limit=30,
    )
    rows = [
        [
            (
                f"📣 {candidate.name[:30]}",
                f"cpg:l:{business_token}:{control._uuid_token(candidate.id)}",
            )
        ]
        for candidate in candidates[:20]
    ]
    rows.extend(
        [
            [("⬅️ Кампании", f"cpg:materials:{business_token}")],
            [("🏠 В кабинет", f"cpj:home:{business_token}")],
        ]
    )
    await callback.answer()
    await control._callback_message(callback).answer(
        "Выберите партнёра. ClientPlatform покажет готовый текст и его отдельную "
        "ссылку для измерения переходов и подтверждённых записей.",
        reply_markup=control._keyboard(rows),
    )


__all__ = [
    "open_partner_campaign_materials",
    "open_partner_materials",
]

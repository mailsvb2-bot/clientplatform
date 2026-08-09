from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from clientplatform.application.owner_booking_journey import (
    connect_public_storefront_customer,
    is_public_storefront_staff,
)
from clientplatform.application.partner_attribution import (
    record_partner_referral_open,
    record_partner_referral_result,
    resolve_partner_referral,
)
from clientplatform.application.partner_runtime import get_partner_candidate_view
from clientplatform.domain.bookings import BookingError
from clientplatform.domain.partners import PartnerNotFound

from . import clientplatform_control as control
from . import clientplatform_simple_experience as simple


log = logging.getLogger(__name__)
_PARTNER_START_PREFIX = "cpg_"
_MAX_REFERRAL_TOKEN_LENGTH = 128


def partner_start_payload(referral_token: str) -> str:
    token = str(referral_token or "").strip()
    if not token or len(token) > _MAX_REFERRAL_TOKEN_LENGTH or ":" in token:
        raise ValueError("invalid partner referral token")
    return f"{_PARTNER_START_PREFIX}{token}"


def partner_deep_link(username: str, referral_token: str) -> str:
    bot_username = str(username or "").strip().lstrip("@")
    if not bot_username:
        raise ValueError("ClientPlatform bot username is required")
    return f"https://t.me/{bot_username}?start={partner_start_payload(referral_token)}"


async def _bot_username(event: CallbackQuery | Message) -> str:
    bot = await event.bot.get_me()
    username = str(getattr(bot, "username", "") or "").strip()
    if not username:
        raise RuntimeError("ClientPlatform bot requires a public username")
    return username


def _referral_token_from_start(message: Message) -> str | None:
    payload = control._start_payload(message)
    if not payload.startswith(_PARTNER_START_PREFIX):
        return None
    token = payload.removeprefix(_PARTNER_START_PREFIX).strip()
    if not token or len(token) > _MAX_REFERRAL_TOKEN_LENGTH or ":" in token:
        return ""
    return token


async def dispatch_partner_referral_start(
    message: Message,
    state: FSMContext,
    *,
    user_id: int,
    managed_bot_business_id: str | None,
) -> bool:
    """Open a partner-attributed storefront without inventing a result."""

    if managed_bot_business_id is not None:
        return False
    referral_token = _referral_token_from_start(message)
    if referral_token is None:
        return False
    if not referral_token:
        await state.clear()
        await message.answer("Партнёрская ссылка недействительна.")
        return True
    try:
        landing = await asyncio.to_thread(
            resolve_partner_referral,
            referral_token=referral_token,
        )
    except PartnerNotFound:
        await state.clear()
        await message.answer("Партнёрская ссылка больше не активна.")
        return True

    if await asyncio.to_thread(
        is_public_storefront_staff,
        business_id=landing.business_id,
        telegram_user_id=user_id,
    ):
        # Owner/staff previews must not inflate partner attribution and must not
        # create a customer identity in their own tenant.
        await state.clear()
        await message.answer(
            "Это партнёрская ссылка Вашего бизнеса. Просмотр сотрудником не "
            "учитывается как партнёрский переход и не создаёт клиентскую карточку.",
            reply_markup=control._keyboard(
                [[("🏠 В мой кабинет", f"cpj:home:{control._uuid_token(landing.business_id)}")]]
            ),
        )
        return True

    user = message.from_user
    link = await asyncio.to_thread(
        connect_public_storefront_customer,
        business_id=landing.business_id,
        telegram_user_id=user_id,
        username=None if user is None else user.username,
        display_name=None if user is None else user.full_name,
    )
    await asyncio.to_thread(
        record_partner_referral_open,
        referral_token=referral_token,
    )
    await state.clear()
    slots = await asyncio.to_thread(
        control.list_customer_booking_slots,
        telegram_user_id=user_id,
        business_id=landing.business_id,
    )
    if not slots:
        await message.answer(
            f"{link.business_name}\n\n"
            "Вы пришли по партнёрской рекомендации. Свободного времени сейчас нет, "
            "но переход сохранён корректно — результатом он не считается."
        )
        return True

    lines = "\n".join(
        f"• {slot.offering_title} — {slot.local_start}, {slot.slot.duration_minutes} минут"
        for slot in slots[:12]
    )
    await message.answer(
        f"{link.business_name}\n\n"
        "Вы пришли по партнёрской рекомендации.\n\n"
        f"Доступно для записи:\n{lines}\n\n"
        "Результат партнёрства будет засчитан только после успешной записи.",
        reply_markup=control._keyboard(
            [
                [
                    (
                        f"{slot.local_start} · {slot.offering_title[:20]}",
                        f"cpg:b:{referral_token}:{control._uuid_token(slot.slot.id)}",
                    )
                ]
                for slot in slots[:12]
            ]
        ),
    )
    return True


@simple.router.callback_query(F.data.startswith("cpg:l:"))
async def show_partner_material(callback: CallbackQuery) -> None:
    _, _, business_token, candidate_token = str(callback.data).split(":", 3)
    actor = await control._actor(
        int(callback.from_user.id),
        control._token_uuid(business_token),
    )
    view = await asyncio.to_thread(
        get_partner_candidate_view,
        actor=actor,
        candidate_id=control._token_uuid(candidate_token),
    )
    username = await _bot_username(callback)
    link = partner_deep_link(username, view.candidate.referral_token)
    post = f"{view.content.ready_post}\n\nПодробнее: {link}"
    share_url = (
        "https://t.me/share/url?url="
        + quote(link, safe="")
        + "&text="
        + quote(view.content.ready_post[:3000], safe="")
    )
    await callback.answer()
    await control._callback_message(callback).answer(
        "📣 Готовый материал с отдельной партнёрской ссылкой\n\n"
        f"{post[:3900]}\n\n"
        "Переходы по этой ссылке считаются отдельно. Сам переход не считается результатом.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📨 Отправить материал в Telegram", url=share_url)],
                [
                    InlineKeyboardButton(
                        text="⬅️ К партнёру",
                        callback_data=f"cpg:c:{business_token}:{candidate_token}",
                    )
                ],
            ]
        ),
    )


@simple.router.callback_query(F.data.startswith("cpg:b:"))
async def book_partner_referral(callback: CallbackQuery) -> None:
    _, _, referral_token, slot_token = str(callback.data).split(":", 3)
    try:
        landing = await asyncio.to_thread(
            resolve_partner_referral,
            referral_token=referral_token,
        )
        claim = await asyncio.to_thread(
            control.book_customer_slot,
            telegram_user_id=int(callback.from_user.id),
            business_id=landing.business_id,
            slot_id=control._token_uuid(slot_token),
        )
    except (PartnerNotFound, BookingError, ValueError):
        await callback.answer(
            "Это время или партнёрская ссылка больше недоступны",
            show_alert=True,
        )
        return

    # Attribution is written only after canonical booking succeeds and uses the
    # business event identity, not the Telegram user id, as its dedupe key.
    await asyncio.to_thread(
        record_partner_referral_result,
        referral_token=referral_token,
        result_key=f"booking:{claim.slot.slot.id}",
    )
    await callback.answer("Запись подтверждена")
    message = control._callback_message(callback)
    await message.answer(
        f"✅ Вы записаны: {claim.slot.offering_title} — {claim.slot.local_start}, "
        f"{claim.slot.slot.duration_minutes} мин.\n"
        f"Бизнес: {claim.slot.business_name}.\n\n"
        "Источник записи сохранён как партнёрская рекомендация."
    )

    slot = claim.slot.slot
    if all(
        hasattr(slot, name)
        for name in ("starts_at", "ends_at", "business_id", "id")
    ):
        await asyncio.to_thread(
            control.schedule_booking_reminders,
            telegram_user_id=int(callback.from_user.id),
            claim=claim,
        )
        document_sender = getattr(message, "answer_document", None)
        if callable(document_sender):
            calendar = BufferedInputFile(
                control.booking_calendar_ics(claim.slot),
                filename=control.booking_calendar_filename(claim.slot),
            )
            await document_sender(
                calendar,
                caption=(
                    "📅 Нажмите на файл — телефон предложит добавить встречу "
                    "с напоминаниями за 24 часа и за 1 час."
                ),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Добавить в Google Календарь",
                                url=control.google_calendar_url(claim.slot),
                            )
                        ]
                    ]
                ),
            )


__all__ = [
    "book_partner_referral",
    "dispatch_partner_referral_start",
    "partner_deep_link",
    "partner_start_payload",
    "show_partner_material",
]

from __future__ import annotations

"""Compose the narrow Promotion Engine over the existing owner journey."""

from types import ModuleType

from aiogram.types import InlineKeyboardMarkup, Message

from clientplatform.domain.bookings import BookingSlotView

from . import clientplatform_partner_growth as partner_growth  # noqa: F401
from . import clientplatform_partner_materials as partner_materials  # noqa: F401
from . import clientplatform_partner_referral as partner_referral
from . import clientplatform_promotion as promotion


def _owner_keyboard(control: ModuleType, business_id: str) -> InlineKeyboardMarkup:
    token = control._uuid_token(business_id)
    return control._keyboard(
        [
            [("✨ Сделать следующий шаг", f"cps:next:{token}")],
            [
                ("🧰 Мои услуги", f"cpj:services:{token}"),
                ("📅 Мой календарь", f"cpj:calendar:{token}:30"),
            ],
            [
                ("👥 Записи клиентов", f"cpj:bookings:{token}"),
                ("🔗 Моя страница", f"cpj:page:{token}"),
            ],
            [
                ("🚀 Получить клиентов", f"cpj:promote:{token}"),
                ("🤝 Партнёрства", f"cpg:home:{token}"),
            ],
            [("📣 Партнёрские материалы", f"cpg:materials:{token}")],
            [
                ("📣 Рекламные кабинеты", f"cpa:home:{token}"),
                ("📊 Яндекс", f"cpy:a:{token}:30"),
            ],
            [("🔌 Отключить кабинет", f"cpa:disconnects:{token}")],
            [("⚙️ Настройки", f"cps:advanced:{token}")],
        ]
    )


async def _send_publish_receipt(
    control: ModuleType,
    message: Message,
    *,
    slot: BookingSlotView,
    changed: bool = False,
) -> None:
    business_token = control._uuid_token(slot.slot.business_id)
    slot_token = control._uuid_token(slot.slot.id)
    offering_token = control._uuid_token(slot.slot.offering_id)
    title = "Время изменено" if changed else "Готово! Время опубликовано"
    await message.answer(
        f"✅ {title}\n\n"
        f"🧰 {slot.offering_title}\n"
        f"📅 {slot.local_start}\n"
        f"⏱ {slot.slot.duration_minutes} минут\n"
        "🟢 Доступно для записи\n\n"
        "Теперь проверьте карточку глазами клиента или превратите свободное "
        "время в рекламное предложение с измеримым результатом.",
        reply_markup=control._keyboard(
            [
                [("👀 Посмотреть глазами клиента", f"cpj:preview:{business_token}:{slot_token}")],
                [("📅 Открыть мой календарь", f"cpj:calendar:{business_token}:30")],
                [
                    ("📨 Просто отправить", f"cpj:share:{business_token}:{slot_token}"),
                    ("🚀 Получить клиентов", f"cpp:slot:{business_token}:{slot_token}"),
                ],
                [("📣 Отправить в рекламный кабинет", f"cpa:slot:{business_token}:{slot_token}")],
                [
                    ("✏️ Изменить", f"cpj:edit:{business_token}:{slot_token}"),
                    ("➕ Ещё время", f"cpj:add:{business_token}:{offering_token}"),
                ],
                [("🏠 В кабинет", f"cpj:home:{business_token}")],
            ]
        ),
    )


def _replace_promotion_route(
    *,
    owner_module: ModuleType,
    simple_module: ModuleType,
) -> None:
    replaced = 0
    for handler in simple_module.router.callback_query.handlers:
        if getattr(handler, "callback", None) is owner_module.open_promotion:
            object.__setattr__(handler, "callback", promotion.open_promotion_workspace)
            replaced += 1
    if replaced != 1:
        raise RuntimeError(
            f"clientplatform promotion route replacement expected 1 handler, got {replaced}"
        )
    owner_module.open_promotion = promotion.open_promotion_workspace


def install_promotion_engine(
    *,
    owner_module: ModuleType,
    simple_module: ModuleType,
) -> None:
    """Install once without importing BusinesAIOS or adding another booking path."""

    if bool(getattr(owner_module, "_promotion_engine_installed", False)):
        return

    control = owner_module.control
    owner_module._owner_keyboard = lambda business_id: _owner_keyboard(control, business_id)

    async def publish_receipt(
        message: Message,
        *,
        slot: BookingSlotView,
        changed: bool = False,
    ) -> None:
        await _send_publish_receipt(
            control,
            message,
            slot=slot,
            changed=changed,
        )

    owner_module._send_publish_receipt = publish_receipt
    _replace_promotion_route(
        owner_module=owner_module,
        simple_module=simple_module,
    )

    previous_public_dispatch = owner_module._dispatch_public_start

    async def dispatch(
        original,
        message,
        state,
        *,
        user_id: int,
        managed_bot_business_id: str | None,
    ) -> None:
        partner_handled = await partner_referral.dispatch_partner_referral_start(
            message,
            state,
            user_id=user_id,
            managed_bot_business_id=managed_bot_business_id,
        )
        if partner_handled:
            return
        handled = await promotion.dispatch_promotion_start(
            message,
            state,
            user_id=user_id,
            managed_bot_business_id=managed_bot_business_id,
        )
        if handled:
            return
        await previous_public_dispatch(
            original,
            message,
            state,
            user_id=user_id,
            managed_bot_business_id=managed_bot_business_id,
        )

    owner_module._dispatch_public_start = dispatch
    owner_module._promotion_engine_installed = True


__all__ = ["install_promotion_engine"]
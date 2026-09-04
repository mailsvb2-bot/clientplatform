from __future__ import annotations

import asyncio
import importlib
from types import ModuleType
from uuid import uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.existing_bot_onboarding import (
    connect_existing_telegram_bot,
)
from clientplatform.domain.bot_provisioning import (
    BotProvisioningError,
    BotProvisioningProvider,
    BotProvisioningStatus,
    BotProvisioningWebhookConflict,
    ManagedBotProvisioningRequest,
)
from clientplatform.infrastructure.managed_bot_credentials import (
    ManagedBotCredentialError,
)

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_existing_bot_onboarding")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

_ORIGINAL_STATUS_TEXT = None
_ORIGINAL_STATUS_KEYBOARD = None


class ExistingBotSetupState(StatesGroup):
    token = State()


def _business_token(business_id: str) -> str:
    return control._uuid_token(business_id)


def _existing_status_text(request: ManagedBotProvisioningRequest | None) -> str:
    if request is None or request.provider != BotProvisioningProvider.BOTFATHER:
        assert _ORIGINAL_STATUS_TEXT is not None
        return _ORIGINAL_STATUS_TEXT(request)
    if request.status == BotProvisioningStatus.COMPLETED:
        return (
            "Мой Telegram-бот\n\n"
            f"✅ @{request.verified_username or request.requested_username or 'бот'} подключён.\n\n"
            "ClientPlatform сам получает сообщения, отправляет материалы и обслуживает "
            "подключение. Никаких технических настроек Вам больше не нужно."
        )
    if request.status in {BotProvisioningStatus.READY, BotProvisioningStatus.VERIFYING}:
        return (
            "Мой Telegram-бот\n\n"
            "⏳ Проверяю бота и подключаю его к ClientPlatform."
        )
    if request.status == BotProvisioningStatus.FAILED:
        return (
            "Мой Telegram-бот\n\n"
            "⚠️ Не удалось подтвердить доступ к боту.\n\n"
            "Нажмите «Ввести актуальный токен» и пришлите свежий токен из BotFather. "
            "Ничего дополнительно настраивать не нужно."
        )
    if request.status == BotProvisioningStatus.CANCELLED:
        return (
            "Мой Telegram-бот\n\n"
            "Подключение отменено. Можно подключить существующего бота заново."
        )
    return (
        "Мой Telegram-бот\n\n"
        "Если у Вас уже есть Telegram-бот, подключить его можно одним действием: "
        "пришлите его токен из BotFather. Сообщение с токеном будет сразу удалено, "
        "а сам токен сохранён зашифрованно."
    )


def _existing_status_keyboard(
    business_id: str,
    request: ManagedBotProvisioningRequest | None,
) -> InlineKeyboardMarkup:
    assert _ORIGINAL_STATUS_KEYBOARD is not None
    markup = _ORIGINAL_STATUS_KEYBOARD(business_id, request)
    business_token = _business_token(business_id)
    rows: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        buttons: list[InlineKeyboardButton] = []
        for button in row:
            callback_data = str(button.callback_data or "")
            if callback_data.startswith("cpb:n:"):
                buttons.append(
                    InlineKeyboardButton(
                        text="У меня уже есть бот",
                        callback_data=f"cpe:n:{business_token}",
                    )
                )
            elif callback_data.startswith(("cpb:r:", "cpb:v:")):
                buttons.append(
                    InlineKeyboardButton(
                        text="Ввести актуальный токен",
                        callback_data=f"cpe:n:{business_token}",
                    )
                )
            else:
                buttons.append(button)
        rows.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def install_existing_bot_onboarding(bot_setup_module: ModuleType) -> None:
    global _ORIGINAL_STATUS_TEXT, _ORIGINAL_STATUS_KEYBOARD
    if bool(getattr(bot_setup_module, "_existing_bot_onboarding_installed", False)):
        return
    _ORIGINAL_STATUS_TEXT = bot_setup_module._status_text
    _ORIGINAL_STATUS_KEYBOARD = bot_setup_module._status_keyboard
    bot_setup_module._status_text = _existing_status_text
    bot_setup_module._status_keyboard = _existing_status_keyboard
    bot_setup_module._existing_bot_onboarding_installed = True


async def _delete_token_message(message: Message) -> bool:
    try:
        await message.delete()
    except TelegramAPIError:
        return False
    return True


@router.callback_query(F.data.startswith("cpe:n:"))
async def begin_existing_bot_connection(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    business_id = control._token_uuid(str(callback.data).split(":", 2)[2])
    await control._actor(int(callback.from_user.id), business_id)
    await state.clear()
    await state.set_state(ExistingBotSetupState.token)
    await state.update_data(
        business_id=business_id,
        idempotency_key=f"existing-bot-ui-{uuid4().hex}",
    )
    await callback.answer()
    await control._callback_message(callback).answer(
        "Уже есть Telegram-бот? Отлично — новый создавать не нужно.\n\n"
        "1. Откройте @BotFather → /mybots → выберите бота → API Token.\n"
        "2. Пришлите токен сюда одним сообщением.\n\n"
        "Я сразу удалю сообщение, проверю бота и сохраню доступ зашифрованно. "
        "Ничего больше вводить или настраивать не потребуется."
    )


@router.message(ExistingBotSetupState.token)
async def receive_existing_bot_token(
    message: Message,
    state: FSMContext,
) -> None:
    token = str(message.text or "").strip()
    if not await _delete_token_message(message):
        await message.answer(
            "Не удалось безопасно удалить сообщение с токеном, поэтому я не стал "
            "использовать или сохранять его. Удалите это сообщение, обновите токен "
            "в @BotFather и пришлите новый токен ещё раз."
        )
        return
    data = await state.get_data()
    business_id = str(data.get("business_id") or "")
    idempotency_key = str(data.get("idempotency_key") or "")
    try:
        actor = await control._actor(control._user_id(message), business_id)
        completed = await connect_existing_telegram_bot(
            actor=actor,
            token=token,
            idempotency_key=idempotency_key,
        )
    except BotProvisioningWebhookConflict:
        await message.answer(
            "Этот бот уже подключён к другому сервису. Чтобы случайно не сломать "
            "его текущую работу, ClientPlatform ничего не переключал.\n\n"
            "Если хотите перенести именно этого бота сюда, сначала отключите его "
            "в прежнем сервисе, затем пришлите новый актуальный токен ещё раз."
        )
        return
    except (
        BotProvisioningError,
        ManagedBotCredentialError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        await message.answer(
            "Не получилось подтвердить этот токен. Проверьте, что это актуальный "
            "API Token нужного бота из @BotFather, и пришлите его ещё раз.\n\n"
            "Сообщение с токеном снова будет удалено сразу."
        )
        return

    await state.clear()
    await message.answer(
        f"✅ @{completed.verified_username or 'Ваш бот'} подключён к ClientPlatform.\n\n"
        "Готово — больше никаких технических действий не требуется.",
        reply_markup=control._keyboard(
            [[("К управлению бизнесом", f"cpb:b:{_business_token(completed.business_id)}")]]
        ),
    )


__all__ = [
    "ExistingBotSetupState",
    "install_existing_bot_onboarding",
    "router",
]

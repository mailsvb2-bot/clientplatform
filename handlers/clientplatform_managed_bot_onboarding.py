from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
from types import ModuleType
from uuid import uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestManagedBot,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from clientplatform.application.managed_bot_onboarding import (
    begin_telegram_managed_bot_onboarding,
    complete_telegram_managed_bot_onboarding,
    get_pending_telegram_managed_bot_onboarding,
    record_telegram_managed_bot_created,
)
from clientplatform.domain.bot_provisioning import (
    BotProvisioningError,
    BotProvisioningProvider,
    BotProvisioningStatus,
    ManagedBotProvisioningRequest,
)
from clientplatform.infrastructure.managed_bot_credentials import (
    ManagedBotCredentialError,
)

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_managed_bot_onboarding")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

_ORIGINAL_STATUS_TEXT = None
_ORIGINAL_STATUS_KEYBOARD = None


def _auto_onboarding_enabled() -> bool:
    return (
        os.getenv("CLIENTPLATFORM_MANAGED_BOT_AUTO_PROVISIONING_ENABLED") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def _business_token(business_id: str) -> str:
    return control._uuid_token(business_id)


def _request_token(request_id: str) -> str:
    return control._uuid_token(request_id)


def _telegram_request_id(request_id: str) -> int:
    digest = hashlib.sha256(str(request_id).encode("ascii")).digest()
    value = int.from_bytes(digest[:4], byteorder="big", signed=True)
    return value if value != 0 else 1


def _managed_status_text(request: ManagedBotProvisioningRequest | None) -> str:
    if request is None:
        if not _auto_onboarding_enabled():
            return (
                "Мой Telegram-бот\n\n"
                "Автоматическое создание персонального бота сейчас подготавливается. "
                "Если у Вас уже есть отдельный бот, его можно подключить резервным способом."
            )
        return (
            "Мой Telegram-бот\n\n"
            "Создайте персонального бота прямо внутри Telegram. ClientPlatform сам "
            "получит доступ, безопасно сохранит его и подключит бота к Вашему кабинету.\n\n"
            "Токены, BotFather и технические настройки для основного способа не нужны."
        )
    if request.provider != BotProvisioningProvider.TELEGRAM_MANAGED:
        assert _ORIGINAL_STATUS_TEXT is not None
        return _ORIGINAL_STATUS_TEXT(request)
    if request.status == BotProvisioningStatus.AWAITING_SECRET:
        if request.external_bot_id and request.verified_username:
            return (
                "Мой Telegram-бот\n\n"
                f"✅ Telegram уже создал @{request.verified_username}.\n\n"
                "Осталось завершить безопасное подключение. Новый бот создавать не нужно."
            )
        return (
            "Мой Telegram-бот\n\n"
            "⏳ Telegram ещё не передал созданного бота ClientPlatform.\n\n"
            "Нажмите «Создать в Telegram». После подтверждения всё остальное "
            "ClientPlatform выполнит автоматически."
        )
    if request.status in {BotProvisioningStatus.READY, BotProvisioningStatus.VERIFYING}:
        return (
            "Мой Telegram-бот\n\n"
            "⏳ Бот создан. ClientPlatform безопасно завершает подключение. "
            "Токен не показывается и не требуется от Вас."
        )
    if request.status == BotProvisioningStatus.FAILED:
        return (
            "Мой Telegram-бот\n\n"
            "⚠️ Бот создан, но автоматическая проверка не завершилась.\n\n"
            "Можно безопасно повторить проверку — повторный бот создан не будет."
        )
    if request.status == BotProvisioningStatus.CANCELLED:
        return (
            "Мой Telegram-бот\n\n"
            "Предыдущее подключение отменено. Можно создать персонального бота заново."
        )
    return (
        "Мой Telegram-бот\n\n"
        f"✅ Подключён @{request.verified_username or 'персональный бот'}.\n"
        "ClientPlatform хранит доступ в зашифрованном виде и обслуживает бота "
        "единым защищённым gateway. Пользователю не нужно хранить или копировать токен."
    )


def _managed_status_keyboard(
    business_id: str,
    request: ManagedBotProvisioningRequest | None,
) -> InlineKeyboardMarkup:
    business_token = _business_token(business_id)
    if request is not None and request.provider != BotProvisioningProvider.TELEGRAM_MANAGED:
        assert _ORIGINAL_STATUS_KEYBOARD is not None
        return _ORIGINAL_STATUS_KEYBOARD(business_id, request)

    rows: list[list[tuple[str, str]]] = []
    if request is None or request.status == BotProvisioningStatus.CANCELLED:
        if _auto_onboarding_enabled():
            rows.append([("✨ Создать моего бота", f"cpm:n:{business_token}")])
        rows.append([("Подключить существующего бота", f"cpb:n:{business_token}")])
    elif request.status == BotProvisioningStatus.AWAITING_SECRET:
        if request.external_bot_id and request.verified_username:
            rows.append(
                [("🔄 Завершить подключение", f"cpm:r:{business_token}")]
            )
        elif _auto_onboarding_enabled():
            rows.append([("✨ Создать в Telegram", f"cpm:n:{business_token}")])
        rows.append(
            [
                (
                    "Отменить",
                    f"cpb:c:{business_token}:{_request_token(request.id)}",
                )
            ]
        )
    elif request.status in {BotProvisioningStatus.READY, BotProvisioningStatus.FAILED}:
        if request.external_bot_id and request.verified_username:
            rows.append(
                [("🔄 Повторить безопасное подключение", f"cpm:r:{business_token}")]
            )
        rows.append(
            [
                (
                    "Отменить",
                    f"cpb:c:{business_token}:{_request_token(request.id)}",
                )
            ]
        )
    rows.append([("Обновить", f"cpb:o:{business_token}")])
    rows.append([("Вернуться в кабинет", f"cpb:b:{business_token}")])
    return control._keyboard(rows)


def install_managed_bot_onboarding(bot_setup_module: ModuleType) -> None:
    global _ORIGINAL_STATUS_TEXT, _ORIGINAL_STATUS_KEYBOARD
    if bool(getattr(bot_setup_module, "_managed_bot_onboarding_installed", False)):
        return
    _ORIGINAL_STATUS_TEXT = bot_setup_module._status_text
    _ORIGINAL_STATUS_KEYBOARD = bot_setup_module._status_keyboard
    bot_setup_module._status_text = _managed_status_text
    bot_setup_module._status_keyboard = _managed_status_keyboard
    bot_setup_module._managed_bot_onboarding_installed = True


async def _business_name(user_id: int, business_id: str) -> str:
    accesses = await asyncio.to_thread(
        control.list_accessible_businesses,
        user_id=user_id,
    )
    access = next(
        (item for item in accesses if item.business.id == business_id),
        None,
    )
    return "Мой помощник" if access is None else str(access.business.name)[:64]


def _bot_display_name(bot_user) -> str | None:
    return " ".join(
        part
        for part in (
            str(bot_user.first_name or "").strip(),
            str(bot_user.last_name or "").strip(),
        )
        if part
    ) or None


async def _send_success(
    message: Message,
    state: FSMContext,
    completed: ManagedBotProvisioningRequest,
) -> None:
    await state.clear()
    await message.answer(
        f"✅ @{completed.verified_username} подключён к ClientPlatform.\n\n"
        "Теперь клиенты смогут получать материалы и программы через Вашего "
        "персонального бота. Токен уже сохранён зашифрованно — ничего дополнительно "
        "настраивать не нужно.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer(
        "Что дальше?",
        reply_markup=control._keyboard(
            [
                [
                    (
                        "К управлению бизнесом",
                        f"cpb:b:{_business_token(completed.business_id)}",
                    )
                ],
                [
                    (
                        "Статус моего бота",
                        f"cpb:o:{_business_token(completed.business_id)}",
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cpm:n:"))
async def request_managed_bot_creation(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if not _auto_onboarding_enabled():
        await callback.answer(
            "Автоматическое создание бота пока подготавливается",
            show_alert=True,
        )
        return
    try:
        business_token = str(callback.data).split(":", 2)[2]
        business_id = control._token_uuid(business_token)
        user_id = int(callback.from_user.id)
        actor = await control._actor(user_id, business_id)
        identity = await callback.bot.get_me()
        if getattr(identity, "can_manage_bots", None) is not True:
            await callback.answer(
                "Автоматическое создание бота пока не включено для ClientPlatform",
                show_alert=True,
            )
            return
        display_name = await _business_name(user_id, business_id)
        request = await asyncio.to_thread(
            begin_telegram_managed_bot_onboarding,
            actor=actor,
            idempotency_key=f"managed-ui-{uuid4().hex}",
            display_name=display_name,
        )
    except (BotProvisioningError, TelegramAPIError, RuntimeError, TypeError, ValueError):
        await callback.answer(
            "Не удалось начать создание бота. Попробуйте ещё раз.",
            show_alert=True,
        )
        return

    await state.clear()
    await callback.answer()
    await control._callback_message(callback).answer(
        "Открою встроенное создание бота Telegram.\n\n"
        "Подтвердите имя — после этого ClientPlatform подключит бота автоматически. "
        "Никаких токенов копировать не потребуется.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text="✨ Создать моего бота",
                        request_managed_bot=KeyboardButtonRequestManagedBot(
                            request_id=_telegram_request_id(request.id),
                            suggested_name=display_name,
                        ),
                    )
                ]
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
            input_field_placeholder="Нажмите кнопку создания бота",
        ),
    )


@router.callback_query(F.data.startswith("cpm:r:"))
async def resume_managed_bot_connection(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    try:
        business_id = control._token_uuid(str(callback.data).split(":", 2)[2])
        user_id = int(callback.from_user.id)
        actor = await control._actor(user_id, business_id)
        pending = await asyncio.to_thread(
            get_pending_telegram_managed_bot_onboarding,
            user_id=user_id,
        )
        if pending.actor.business_id != actor.business_id:
            raise ValueError("managed bot retry belongs to another business")
        request = pending.request
        if not request.external_bot_id or not request.verified_username:
            raise ValueError("managed bot identity is unavailable for retry")
        token = await callback.bot.get_managed_bot_token(
            user_id=int(request.external_bot_id)
        )
        completed = await complete_telegram_managed_bot_onboarding(
            user_id=user_id,
            external_bot_id=request.external_bot_id,
            username=request.verified_username,
            display_name=request.display_name,
            token=token,
        )
    except (
        BotProvisioningError,
        ManagedBotCredentialError,
        TelegramAPIError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        await callback.answer(
            "Не удалось завершить подключение. Попробуйте ещё раз позже.",
            show_alert=True,
        )
        return
    await callback.answer("Бот подключён")
    await _send_success(control._callback_message(callback), state, completed)


@router.message(F.managed_bot_created)
async def receive_managed_bot_created(
    message: Message,
    state: FSMContext,
) -> None:
    created = message.managed_bot_created
    if created is None:
        return
    bot_user = created.bot
    username = str(bot_user.username or "").strip().lower()
    if not username:
        await message.answer(
            "Telegram создал бота без username, поэтому подключение не завершено.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    user_id = control._user_id(message)
    display_name = _bot_display_name(bot_user)
    try:
        await asyncio.to_thread(
            record_telegram_managed_bot_created,
            user_id=user_id,
            external_bot_id=str(bot_user.id),
            username=username,
            display_name=display_name,
            event_at=message.date,
        )
        token = await message.bot.get_managed_bot_token(user_id=int(bot_user.id))
        completed = await complete_telegram_managed_bot_onboarding(
            user_id=user_id,
            external_bot_id=str(bot_user.id),
            username=username,
            display_name=display_name,
            token=token,
            event_at=message.date,
        )
    except (
        BotProvisioningError,
        ManagedBotCredentialError,
        TelegramAPIError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        await message.answer(
            "Telegram уже создал бота, но ClientPlatform пока не завершил подключение. "
            "Откройте /mybot и нажмите «Завершить подключение» — новый бот создавать "
            "не нужно.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await _send_success(message, state, completed)


__all__ = [
    "install_managed_bot_onboarding",
    "receive_managed_bot_created",
    "request_managed_bot_creation",
    "resume_managed_bot_connection",
    "router",
]

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

from aiogram import F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.ad_connections import (
    complete_yandex_direct_oauth,
    start_yandex_direct_oauth,
)
from clientplatform.domain.ad_connections import AdConnectionError
from clientplatform.integrations.yandex_direct import YandexDirectError
from clientplatform.integrations.yandex_screen_code import (
    screen_code_provider_from_environment,
)

from . import clientplatform_control as control
from . import clientplatform_simple_experience as simple


class YandexScreenCodeState(StatesGroup):
    waiting_code = State()


def _message(callback: CallbackQuery) -> Message:
    return control._callback_message(callback)


def _oauth_state_from_authorization_url(value: str) -> str:
    query = parse_qs(urlparse(str(value or "")).query)
    states = [item.strip() for item in query.get("state", []) if item.strip()]
    if len(states) != 1:
        raise ValueError("Yandex OAuth authorization URL must contain one state")
    return states[0]


def _confirmation_code(value: str | None) -> str:
    code = "".join(str(value or "").split())
    if len(code) != 7 or not code.isascii() or not code.isdigit():
        raise ValueError("Yandex OAuth confirmation code must contain seven digits")
    return code


@simple.router.callback_query(F.data.startswith("cpa:connect:"))
async def connect_yandex_direct_screen_code(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    try:
        business_token = str(callback.data).split(":", 2)[2]
        business_id = control._token_uuid(business_token)
        actor = await control._actor(int(callback.from_user.id), business_id)
        provider = screen_code_provider_from_environment()
        start = await asyncio.to_thread(
            start_yandex_direct_oauth,
            actor=actor,
            provider=provider,
        )
        oauth_state = _oauth_state_from_authorization_url(start.authorization_url)
    except (AdConnectionError, YandexDirectError, RuntimeError, ValueError):
        await callback.answer("Не удалось начать подключение", show_alert=True)
        return

    await state.set_state(YandexScreenCodeState.waiting_code)
    await state.set_data(
        {
            "business_id": business_id,
            "business_token": business_token,
            "oauth_state": oauth_state,
            "oauth_user_id": int(callback.from_user.id),
        }
    )
    await callback.answer()
    await _message(callback).answer(
        "🔐 Подключение Яндекс Директа\n\n"
        "1. Откройте официальный экран Яндекса.\n"
        "2. Выберите рекламный аккаунт и разрешите доступ.\n"
        "3. Яндекс покажет семизначный код.\n"
        "4. Скопируйте код и отправьте его сюда одним сообщением.\n\n"
        "Код действует 10 минут. Пароль и OAuth-токен ClientPlatform не просит. "
        "Сообщение с кодом бот постарается удалить сразу после получения.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть Яндекс и получить код",
                        url=start.authorization_url,
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data=f"cpa:yandex-cancel:{business_token}",
                    )
                ],
            ]
        ),
    )


@simple.router.callback_query(F.data.startswith("cpa:yandex-cancel:"))
async def cancel_yandex_direct_screen_code(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    await state.clear()
    await callback.answer("Подключение отменено")
    await _message(callback).answer(
        "Подключение Яндекс Директа отменено. Временная сессия перестанет действовать автоматически.",
        reply_markup=control._keyboard(
            [[("Вернуться к рекламным кабинетам", f"cpa:home:{business_token}")]]
        ),
    )


async def _restart_message(message: Message, state: FSMContext, *, reason: str) -> None:
    await state.clear()
    await message.answer(
        "Не удалось подтвердить доступ. "
        f"{reason} Начните подключение Яндекс Директа заново."
    )


async def _erase_confirmation_code_message(message: Message) -> None:
    delete = getattr(message, "delete", None)
    if not callable(delete):
        return
    try:
        await delete()
    except (TelegramAPIError, OSError, RuntimeError):
        return


@simple.router.message(YandexScreenCodeState.waiting_code)
async def complete_yandex_direct_screen_code(
    message: Message,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    try:
        initiating_user_id = int(data["oauth_user_id"])
        oauth_state = str(data["oauth_state"])
    except (KeyError, TypeError, ValueError):
        await _restart_message(
            message,
            state,
            reason="Сессия подключения потеряна.",
        )
        return
    if control._user_id(message) != initiating_user_id:
        await _restart_message(
            message,
            state,
            reason="Сессия принадлежит другому пользователю.",
        )
        return
    try:
        code = _confirmation_code(message.text)
    except ValueError:
        await message.answer(
            "Код должен состоять ровно из семи цифр. Скопируйте код со страницы Яндекса и отправьте его ещё раз."
        )
        return

    await _erase_confirmation_code_message(message)
    try:
        provider = screen_code_provider_from_environment()
        completion = await asyncio.to_thread(
            complete_yandex_direct_oauth,
            state=oauth_state,
            code=code,
            provider=provider,
        )
    except (AdConnectionError, YandexDirectError, RuntimeError, ValueError):
        await _restart_message(
            message,
            state,
            reason="Код мог истечь или уже быть использован.",
        )
        return

    business_token = str(data.get("business_token") or "").strip()
    await state.clear()
    rows = []
    if business_token:
        rows.append(
            [("Вернуться к рекламным кабинетам", f"cpa:home:{business_token}")]
        )
    await message.answer(
        "✅ Яндекс Директ подключён\n\n"
        f"Кабинет: {completion.connection.external_login}\n"
        "Теперь ClientPlatform может безопасно читать кампании и готовить рекламные действия в пределах Ваших подтверждений.",
        reply_markup=control._keyboard(rows) if rows else None,
    )


__all__ = [
    "YandexScreenCodeState",
    "cancel_yandex_direct_screen_code",
    "complete_yandex_direct_screen_code",
    "connect_yandex_direct_screen_code",
]

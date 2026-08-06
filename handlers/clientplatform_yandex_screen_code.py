from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.ad_connections import (
    complete_yandex_direct_oauth,
    start_yandex_direct_oauth,
)
from clientplatform.domain.ad_connections import AdConnectionError
from clientplatform.integrations.yandex_direct import YandexDirectError

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
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    try:
        start = await asyncio.to_thread(start_yandex_direct_oauth, actor=actor)
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
        "Код действует 10 минут. Пароль и OAuth-токен ClientPlatform не просит.",
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
                        callback_data=f"cpa:home:{business_token}",
                    )
                ],
            ]
        ),
    )


@simple.router.message(YandexScreenCodeState.waiting_code)
async def complete_yandex_direct_screen_code(
    message: Message,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    try:
        if control._user_id(message) != int(data["oauth_user_id"]):
            raise ValueError("OAuth user changed")
        code = _confirmation_code(message.text)
        completion = await asyncio.to_thread(
            complete_yandex_direct_oauth,
            state=str(data["oauth_state"]),
            code=code,
        )
    except ValueError:
        await message.answer(
            "Код должен состоять ровно из семи цифр. Скопируйте код со страницы Яндекса и отправьте его ещё раз."
        )
        return
    except (KeyError, TypeError, AdConnectionError, YandexDirectError, RuntimeError):
        await state.clear()
        await message.answer(
            "Не удалось подтвердить доступ. Код мог истечь или уже быть использован. Начните подключение Яндекс Директа заново."
        )
        return

    await state.clear()
    await message.answer(
        "✅ Яндекс Директ подключён\n\n"
        f"Кабинет: {completion.connection.external_login}\n"
        "Теперь ClientPlatform может безопасно читать кампании и готовить рекламные действия в пределах Ваших подтверждений.",
        reply_markup=control._keyboard(
            [[("Вернуться к рекламным кабинетам", f"cpa:home:{data['business_token']}")]]
        ),
    )


__all__ = [
    "YandexScreenCodeState",
    "complete_yandex_direct_screen_code",
    "connect_yandex_direct_screen_code",
]

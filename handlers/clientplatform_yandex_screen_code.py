from __future__ import annotations

import asyncio
import logging
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
from clientplatform.application.ad_oauth_sessions import cancel_yandex_direct_oauth
from clientplatform.domain.ad_connections import AdConnectionError
from clientplatform.integrations.yandex_direct import YandexDirectError
from clientplatform.integrations.yandex_screen_code import (
    normalize_yandex_confirmation_code,
    normalize_yandex_login_hint,
    screen_code_provider_from_environment,
)

from . import clientplatform_control as control
from . import clientplatform_goal_first_safety as goal_contract
from . import clientplatform_simple_experience as simple


logger = logging.getLogger(__name__)
_RETRYABLE_CONFIRMATION_CODE_ERRORS = frozenset(
    {"provider_invalid_grant", "provider_bad_verification_code"}
)


class YandexScreenCodeState(StatesGroup):
    waiting_login_hint = State()
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
    try:
        return normalize_yandex_confirmation_code(value)
    except YandexDirectError as exc:
        raise ValueError("Yandex OAuth confirmation code is invalid") from exc


def _incoming_confirmation_code(message: Message) -> str | None:
    text = getattr(message, "text", None)
    if isinstance(text, str):
        return text
    caption = getattr(message, "caption", None)
    return caption if isinstance(caption, str) else None


def _ad_connection_failure_reason(exc: AdConnectionError) -> str:
    code = str(exc).strip()
    if code == "direct_account_owned_by_another_business":
        return (
            "Этот рекламный кабинет уже подключён к другому рабочему пространству "
            "ClientPlatform. Чтобы перенести кабинет, сначала полностью отзовите "
            "подключение в прежнем пространстве."
        )
    if code == "direct_identity_reverification_pending":
        return (
            "Идёт безопасная переверификация ранее подключённых кабинетов. "
            "Новый кабинет пока нельзя закрепить за другим пространством."
        )
    if code == "direct_identity_reverification_ambiguous":
        return (
            "Найдено несколько старых подключений, поэтому кабинет нельзя выбрать "
            "однозначно. Сначала завершите переверификацию старых подключений."
        )
    if code == "direct_identity_reverification_required":
        return (
            "Это старое подключение нужно подтвердить заново через Яндекс Директ, "
            "чтобы определить реальный рекламный кабинет."
        )
    return "Код мог истечь или уже быть использован."


def _provider_failure_reason(exc: YandexDirectError) -> str:
    if exc.code.startswith("direct_identity_"):
        return (
            "Код был передан в OAuth, но выбранный аккаунт не удалось подтвердить "
            "как доступный аккаунт Яндекс Директа. Выберите другой аккаунт Яндекса "
            "или проверьте, что у выбранного логина есть доступ к Директу."
        )
    return (
        "Яндекс OAuth отклонил обмен кода. Получите новый код в новой попытке "
        "подключения и убедитесь, что авторизуетесь под нужным аккаунтом Яндекса."
    )


def _provider_failure_stage(exc: YandexDirectError) -> str:
    return "direct_identity" if exc.code.startswith("direct_identity_") else "token_exchange"


def _account_mode_keyboard(business_token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Выбрать аккаунт на Яндексе",
                    callback_data=f"cpa:yandex-account-auto:{business_token}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Указать другой логин / e-mail",
                    callback_data=f"cpa:yandex-account-hint:{business_token}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"cpa:home:{business_token}",
                )
            ],
        ]
    )


def _connected_account_keyboard(business_token: str) -> InlineKeyboardMarkup:
    return control._keyboard(
        [
            [
                (
                    goal_contract.ACQUIRE_CLIENTS.label,
                    goal_contract.ACQUIRE_CLIENTS.callback(business_token),
                )
            ],
            [("⚙️ Управление Яндекс Директом", f"cpa:home:{business_token}")],
        ]
    )


def _connected_account_message(external_login: str) -> str:
    return (
        "✅ Яндекс Директ подключён\n\n"
        f"Кабинет: {external_login}\n\n"
        "Что делать дальше:\n"
        f"1. Нажмите «{goal_contract.ACQUIRE_CLIENTS.label}».\n"
        "2. ClientPlatform проверит свободное время и спросит только то, чего нельзя "
        "определить безопасно — например, где искать клиентов и какой бюджет допустим.\n"
        "3. Вы увидите подготовленную рекламу и условия до запуска.\n\n"
        "Без отдельного подтверждения запуск с расходами не выполняется."
    )


async def _send_authorization_prompt(
    message: Message,
    *,
    business_token: str,
    authorization_url: str,
    targeted_account: bool,
) -> None:
    account_note = (
        "Яндекс получил указанный Вами логин/e-mail как подсказку выбора аккаунта. "
        "Перед разрешением доступа всё равно проверьте выбранный аккаунт.\n\n"
        if targeted_account
        else "На экране Яндекса обязательно проверьте, какой аккаунт выбран. Если он не тот — отмените попытку и используйте «Указать другой логин / e-mail».\n\n"
    )
    await message.answer(
        "🔐 Подключение Яндекс Директа\n\n"
        + account_note
        + "1. Откройте официальный экран Яндекса.\n"
        "2. Выберите нужный аккаунт и разрешите доступ.\n"
        "3. Яндекс покажет код подтверждения.\n"
        "4. Скопируйте сам код и отправьте его сюда одним сообщением.\n\n"
        "Код действует 10 минут. Пароль и OAuth-токен ClientPlatform не просит. "
        "Сообщение с кодом бот постарается удалить сразу после получения.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть Яндекс и получить код",
                        url=authorization_url,
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


@simple.router.callback_query(F.data.startswith("cpa:connect:"))
async def yandex_direct_onboarding(callback: CallbackQuery) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    control._token_uuid(business_token)
    await callback.answer()
    await _message(callback).answer(
        "📣 Яндекс Директ\n\n"
        "Подключается только Ваш собственный рекламный кабинет. Общего кабинета "
        "ClientPlatform нет: доступ и ownership всегда привязаны к конкретному "
        "advertiser в Яндекс Директе.",
        reply_markup=control._keyboard(
            [
                [("🔐 Подключить мой кабинет", f"cpa:connect-mine:{business_token}")],
                [("🆕 У меня ещё нет кабинета", f"cpa:no-account:{business_token}")],
                [("⬅️ Назад", f"cpa:home:{business_token}")],
            ]
        ),
    )


@simple.router.callback_query(F.data.startswith("cpa:no-account:"))
async def yandex_direct_no_account(callback: CallbackQuery) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    control._token_uuid(business_token)
    await callback.answer()
    await _message(callback).answer(
        "🆕 У Вас ещё нет кабинета\n\n"
        "Создайте собственный кабинет на официальном сайте Яндекс Директа. После "
        "создания вернитесь сюда и подключите именно его — ClientPlatform не "
        "подставляет общий или чужой рекламный аккаунт.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Создать кабинет в Яндекс Директе",
                        url="https://direct.yandex.ru/",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔐 Подключить мой кабинет",
                        callback_data=f"cpa:connect-mine:{business_token}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data=f"cpa:home:{business_token}",
                    )
                ],
            ]
        ),
    )


@simple.router.callback_query(F.data.startswith("cpa:connect-mine:"))
async def choose_yandex_account_mode(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    try:
        await control._actor(int(callback.from_user.id), business_id)
    except (AdConnectionError, RuntimeError, ValueError):
        await callback.answer("Не удалось открыть подключение", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await _message(callback).answer(
        "👤 Какой аккаунт Яндекса подключить?\n\n"
        "Если у Вас несколько аккаунтов, не полагайтесь на автоматически выбранный. "
        "Можно либо выбрать аккаунт на экране Яндекса, либо явно указать его логин/e-mail.",
        reply_markup=_account_mode_keyboard(business_token),
    )


@simple.router.callback_query(F.data.startswith("cpa:yandex-account-auto:"))
async def connect_yandex_direct_account_picker(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await connect_yandex_direct_screen_code(callback, state)


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
    await _send_authorization_prompt(
        _message(callback),
        business_token=business_token,
        authorization_url=start.authorization_url,
        targeted_account=False,
    )


@simple.router.callback_query(F.data.startswith("cpa:yandex-account-hint:"))
async def request_yandex_account_hint(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    try:
        await control._actor(int(callback.from_user.id), business_id)
    except (AdConnectionError, RuntimeError, ValueError):
        await callback.answer("Не удалось открыть подключение", show_alert=True)
        return
    await state.set_state(YandexScreenCodeState.waiting_login_hint)
    await state.set_data(
        {
            "business_id": business_id,
            "business_token": business_token,
            "oauth_user_id": int(callback.from_user.id),
        }
    )
    await callback.answer()
    await _message(callback).answer(
        "Введите логин Яндекса или адрес Яндекс Почты того аккаунта, который нужно подключить.\n\n"
        "Это только подсказка официальному OAuth Яндекса: окончательно рекламный аккаунт ClientPlatform подтвердит через API Яндекс Директа.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data=f"cpa:yandex-cancel:{business_token}",
                    )
                ]
            ]
        ),
    )


async def _abort_account_hint(
    message: Message,
    state: FSMContext,
) -> None:
    await state.clear()
    await message.answer(
        "Не удалось начать подключение для выбранного аккаунта. "
        "Вернитесь в рекламные кабинеты и повторите попытку."
    )


@simple.router.message(YandexScreenCodeState.waiting_login_hint)
async def receive_yandex_account_hint(
    message: Message,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    try:
        initiating_user_id = int(data["oauth_user_id"])
        business_token = str(data["business_token"])
        business_id = control._token_uuid(business_token)
        if control._user_id(message) != initiating_user_id:
            raise ValueError("OAuth account selection belongs to another user")
        login_hint = normalize_yandex_login_hint(message.text)
        actor = await control._actor(initiating_user_id, business_id)
        provider = screen_code_provider_from_environment(login_hint=login_hint)
        start = await asyncio.to_thread(
            start_yandex_direct_oauth,
            actor=actor,
            provider=provider,
        )
        oauth_state = _oauth_state_from_authorization_url(start.authorization_url)
    except YandexDirectError:
        await message.answer("Введите один логин Яндекса или e-mail без пробелов.")
        return
    except (AdConnectionError, RuntimeError, ValueError):
        await _abort_account_hint(message, state)
        return
    except (KeyError, TypeError):
        await _abort_account_hint(message, state)
        return

    await state.set_state(YandexScreenCodeState.waiting_code)
    await state.set_data(
        {
            "business_id": business_id,
            "business_token": business_token,
            "oauth_state": oauth_state,
            "oauth_user_id": initiating_user_id,
        }
    )
    await _erase_confirmation_code_message(message)
    await _send_authorization_prompt(
        message,
        business_token=business_token,
        authorization_url=start.authorization_url,
        targeted_account=True,
    )


@simple.router.callback_query(F.data.startswith("cpa:yandex-cancel:"))
async def cancel_yandex_direct_screen_code(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    try:
        business_token = str(callback.data).split(":", 2)[2]
        if str(data["business_token"]) != business_token:
            raise ValueError("OAuth business token does not match the callback")
        initiating_user_id = int(data["oauth_user_id"])
        if int(callback.from_user.id) != initiating_user_id:
            raise ValueError("OAuth session belongs to another user")
        oauth_state = str(data.get("oauth_state") or "").strip()
        if oauth_state:
            business_id = control._token_uuid(business_token)
            actor = await control._actor(initiating_user_id, business_id)
            await asyncio.to_thread(
                cancel_yandex_direct_oauth,
                actor=actor,
                state=oauth_state,
            )
    except (AdConnectionError, KeyError, RuntimeError, TypeError, ValueError):
        await callback.answer("Не удалось отменить подключение", show_alert=True)
        return

    await state.clear()
    await callback.answer("Подключение отменено")
    await _message(callback).answer(
        "Подключение Яндекс Директа отменено. Временная OAuth-сессия закрыта и больше не может быть использована.",
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


async def _retry_confirmation_code_message(message: Message) -> None:
    await message.answer(
        "Яндекс не принял этот код подтверждения. Скопируйте код со страницы Яндекса "
        "ещё раз и отправьте сюда одним сообщением. Если код истёк, снова откройте "
        "кнопку «Открыть Яндекс и получить код» в предыдущем сообщении. "
        "Текущая сессия подключения пока сохранена."
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
        code = _confirmation_code(_incoming_confirmation_code(message))
    except ValueError:
        await message.answer(
            "Отправьте только код подтверждения со страницы Яндекса одним сообщением, без пояснений."
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
    except AdConnectionError as exc:
        await _restart_message(
            message,
            state,
            reason=_ad_connection_failure_reason(exc),
        )
        return
    except YandexDirectError as exc:
        logger.warning(
            "Yandex Direct OAuth completion failed stage=%s provider_code=%s retryable=%s",
            _provider_failure_stage(exc),
            exc.code,
            exc.retryable,
        )
        if exc.code in _RETRYABLE_CONFIRMATION_CODE_ERRORS:
            await _retry_confirmation_code_message(message)
            return
        await _restart_message(
            message,
            state,
            reason=_provider_failure_reason(exc),
        )
        return
    except (RuntimeError, ValueError) as exc:
        logger.warning(
            "Yandex Direct OAuth completion failed stage=internal exception_type=%s",
            type(exc).__name__,
        )
        await _restart_message(
            message,
            state,
            reason="Внутренний этап подключения не завершился безопасно.",
        )
        return

    business_token = str(data.get("business_token") or "").strip()
    await state.clear()
    await message.answer(
        _connected_account_message(completion.connection.external_login),
        reply_markup=(
            _connected_account_keyboard(business_token) if business_token else None
        ),
    )


__all__ = [
    "YandexScreenCodeState",
    "cancel_yandex_direct_screen_code",
    "choose_yandex_account_mode",
    "complete_yandex_direct_screen_code",
    "connect_yandex_direct_account_picker",
    "connect_yandex_direct_screen_code",
    "receive_yandex_account_hint",
    "request_yandex_account_hint",
    "yandex_direct_no_account",
    "yandex_direct_onboarding",
]

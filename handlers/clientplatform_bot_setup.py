from __future__ import annotations

import asyncio
import importlib
import re
from types import ModuleType
from uuid import uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.bot_provisioning import (
    cancel_botfather_provisioning,
    create_botfather_provisioning,
    finalize_botfather_provisioning,
    get_bot_provisioning,
    list_bot_provisioning_requests,
    submit_botfather_secret_references,
)
from clientplatform.application.tenancy import list_accessible_businesses
from clientplatform.domain.bot_provisioning import (
    BotProvisioningInvariantViolation,
    BotProvisioningNotFound,
    BotProvisioningStatus,
    BotProvisioningVerificationFailed,
    ManagedBotProvisioningRequest,
    normalize_requested_username,
)
from clientplatform.domain.connections import ConnectionError, ConnectionInvariantViolation
from clientplatform.domain.tenancy import TenancyError

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_bot_setup")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())


class ManagedBotSetupState(StatesGroup):
    username = State()
    token_reference = State()
    # Kept only to recover chats that were left on the historical webhook step.
    webhook_reference = State()


class RawSecretInputError(ValueError):
    """A user appears to have pasted secret material instead of a reference."""


_ENV_NAME_RE = re.compile(r"CLIENTPLATFORM_SECRET_[A-Z0-9_]{4,96}")
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")
_STATUS_LABELS = {
    BotProvisioningStatus.AWAITING_SECRET: "нужно указать ссылку на токен",
    BotProvisioningStatus.READY: "готов к проверке",
    BotProvisioningStatus.VERIFYING: "идёт проверка",
    BotProvisioningStatus.COMPLETED: "подключён через polling",
    BotProvisioningStatus.FAILED: "нужно исправить и повторить",
    BotProvisioningStatus.CANCELLED: "отменён",
}
_ERROR_LABELS = {
    "telegram_verification_failed": "Telegram не подтвердил бота или polling-подготовку.",
    "telegram_identity_mismatch": "Имя подтверждённого бота не совпало с ожидаемым.",
    "provisioner_failed": "Проверка Telegram завершилась технической ошибкой.",
    "provisioning_commit_failed": "Маршрут polling не удалось сохранить.",
    "verification_cancelled": "Проверка была прервана.",
    "commit_cancelled": "Сохранение подключения было прервано.",
    "verification_lease_expired": "Предыдущая проверка прервалась и может быть повторена.",
}


def _secret_reference_from_input(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("secret reference is empty")
    if _TELEGRAM_TOKEN_RE.search(raw):
        raise RawSecretInputError("raw Telegram token is forbidden")
    if raw.startswith("secret://env/"):
        name = raw.removeprefix("secret://env/").strip().upper()
    else:
        if ":" in raw or "/" in raw or " " in raw:
            raise RawSecretInputError("secret-like value is forbidden")
        name = raw.upper()
    if not _ENV_NAME_RE.fullmatch(name):
        raise ValueError("invalid ClientPlatform secret environment name")
    return f"secret://env/{name}"


def _request_token(request_id: str) -> str:
    return control._uuid_token(request_id)


def _business_token(business_id: str) -> str:
    return control._uuid_token(business_id)


def _callback_ids(data: str, *, expected_parts: int) -> list[str]:
    parts = str(data or "").split(":")
    if len(parts) != expected_parts:
        raise ValueError("invalid managed bot callback")
    return parts


def _status_text(request: ManagedBotProvisioningRequest | None) -> str:
    if request is None:
        return (
            "Мой Telegram-бот\n\n"
            "Персональный бот ещё не подключён. После безопасной проверки клиенты "
            "смогут получать программы и сообщения от имени Вашего бота.\n\n"
            "Telegram работает только через polling. Токен нельзя отправлять в чат: "
            "мастер принимает только имя переменной secret-store вида "
            "CLIENTPLATFORM_SECRET_... ."
        )
    lines = [
        "Мой Telegram-бот",
        "",
        f"Статус: {_STATUS_LABELS[request.status]}.",
        f"Ожидаемое имя: @{request.requested_username or 'не указано'}.",
        f"Попыток проверки: {request.attempts}.",
    ]
    if request.status == BotProvisioningStatus.COMPLETED:
        lines.extend(
            [
                f"Подключённый бот: @{request.verified_username or request.requested_username}.",
                f"Telegram bot ID: {request.external_bot_id}.",
                "Транспорт: polling. Webhook для этого бота отключён.",
                "Токен хранится вне ClientPlatform.",
            ]
        )
    elif request.last_error_code:
        lines.append(
            _ERROR_LABELS.get(
                request.last_error_code,
                "Проверка не завершена. Исправьте ссылку и повторите.",
            )
        )
    if request.status in {
        BotProvisioningStatus.AWAITING_SECRET,
        BotProvisioningStatus.READY,
        BotProvisioningStatus.FAILED,
    }:
        lines.extend(
            [
                "",
                "Указывайте только имя переменной CLIENTPLATFORM_SECRET_... — не значение токена.",
            ]
        )
    return "\n".join(lines)


def _status_keyboard(
    business_id: str,
    request: ManagedBotProvisioningRequest | None,
) -> InlineKeyboardMarkup:
    business_token = _business_token(business_id)
    rows: list[list[tuple[str, str]]] = []
    if request is None or request.status == BotProvisioningStatus.CANCELLED:
        rows.append([("Подключить бота", f"cpb:n:{business_token}")])
    elif request.status == BotProvisioningStatus.AWAITING_SECRET:
        request_token = _request_token(request.id)
        rows.extend(
            [
                [("Указать ссылку на токен", f"cpb:r:{business_token}:{request_token}")],
                [("Отменить", f"cpb:c:{business_token}:{request_token}")],
            ]
        )
    elif request.status == BotProvisioningStatus.READY:
        request_token = _request_token(request.id)
        rows.extend(
            [
                [("Проверить и подключить", f"cpb:v:{business_token}:{request_token}")],
                [("Изменить ссылку", f"cpb:r:{business_token}:{request_token}")],
                [("Отменить", f"cpb:c:{business_token}:{request_token}")],
            ]
        )
    elif request.status == BotProvisioningStatus.FAILED:
        request_token = _request_token(request.id)
        rows.extend(
            [
                [("Повторить проверку", f"cpb:v:{business_token}:{request_token}")],
                [("Исправить ссылку", f"cpb:r:{business_token}:{request_token}")],
                [("Отменить", f"cpb:c:{business_token}:{request_token}")],
            ]
        )
    rows.append([("Обновить", f"cpb:o:{business_token}")])
    rows.append([("Вернуться в кабинет", f"cpb:b:{business_token}")])
    return control._keyboard(rows)


def install_dashboard_button(control_module: ModuleType) -> None:
    """Add the entry point without rewriting the control handler."""

    if bool(getattr(control_module, "_managed_bot_dashboard_installed", False)):
        return
    original = control_module._dashboard_keyboard

    def dashboard_with_bot(business_id: str, capabilities: list[object]) -> InlineKeyboardMarkup:
        markup = original(business_id, capabilities)
        button = InlineKeyboardButton(
            text="Мой Telegram-бот",
            callback_data=f"cpb:o:{_business_token(business_id)}",
        )
        return InlineKeyboardMarkup(inline_keyboard=[*markup.inline_keyboard, [button]])

    control_module._dashboard_keyboard = dashboard_with_bot
    control_module._managed_bot_dashboard_installed = True


async def _actor(user_id: int, business_id: str):
    return await control._actor(user_id, business_id)


async def _latest_request(*, user_id: int, business_id: str):
    actor = await _actor(user_id, business_id)
    requests = await asyncio.to_thread(
        list_bot_provisioning_requests,
        actor=actor,
        limit=20,
    )
    return actor, (requests[0] if requests else None)


async def _send_status(
    message: Message,
    *,
    user_id: int,
    business_id: str,
) -> None:
    _, request = await _latest_request(user_id=user_id, business_id=business_id)
    await message.answer(
        _status_text(request),
        reply_markup=_status_keyboard(business_id, request),
    )


async def _delete_sensitive_message(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError:
        return


@router.message(Command("mybot"))
async def open_my_bot_command(message: Message, state: FSMContext) -> None:
    user_id = control._user_id(message)
    accesses = await asyncio.to_thread(list_accessible_businesses, user_id=user_id)
    await state.clear()
    if not accesses:
        await message.answer("Сначала создайте бизнес через /start.")
        return
    if len(accesses) == 1:
        await _send_status(message, user_id=user_id, business_id=accesses[0].business.id)
        return
    await message.answer(
        "Для какого бизнеса открыть настройки Telegram-бота?",
        reply_markup=control._keyboard(
            [
                [(access.business.name, f"cpb:o:{_business_token(access.business.id)}")]
                for access in accesses
            ]
        ),
    )


@router.callback_query(F.data.startswith("cpb:o:"))
async def open_bot_status(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token = _callback_ids(str(callback.data), expected_parts=3)
    business_id = control._token_uuid(business_token)
    await callback.answer()
    await state.clear()
    await _send_status(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cpb:b:"))
async def back_to_business(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token = _callback_ids(str(callback.data), expected_parts=3)
    business_id = control._token_uuid(business_token)
    await callback.answer()
    await state.clear()
    await control._send_dashboard(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cpb:n:"))
async def begin_bot_setup(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token = _callback_ids(str(callback.data), expected_parts=3)
    business_id = control._token_uuid(business_token)
    await _actor(int(callback.from_user.id), business_id)
    await state.set_state(ManagedBotSetupState.username)
    await state.update_data(
        business_id=business_id,
        idempotency_key=f"owner-ui-{uuid4().hex}",
    )
    await callback.answer()
    await control._callback_message(callback).answer(
        "Шаг 1 из 2. Напишите username бота, созданного через BotFather.\n\n"
        "Например: @my_business_bot. Имя должно оканчиваться на bot.\n\n"
        "Токен сюда не отправляйте."
    )


@router.message(ManagedBotSetupState.username)
async def receive_bot_username(message: Message, state: FSMContext) -> None:
    try:
        username = normalize_requested_username(str(message.text or ""))
    except ValueError:
        await message.answer("Не похоже на username Telegram-бота. Пример: @my_business_bot.")
        return
    if username is None:
        await message.answer("Username бота обязателен.")
        return
    data = await state.get_data()
    business_id = str(data["business_id"])
    actor = await _actor(control._user_id(message), business_id)
    request = await asyncio.to_thread(
        create_botfather_provisioning,
        actor=actor,
        idempotency_key=str(data["idempotency_key"]),
        requested_username=username,
        display_name=username,
    )
    await state.update_data(request_id=request.id)
    await state.set_state(ManagedBotSetupState.token_reference)
    await message.answer(
        "Шаг 2 из 2. Сохраните токен BotFather прямо в secret-store.\n\n"
        "Затем напишите только имя переменной, например:\n"
        "CLIENTPLATFORM_SECRET_TELEGRAM_MY_PRACTICE\n\n"
        "Не вставляйте значение токена. Отдельный webhook-секрет не нужен."
    )


@router.callback_query(F.data.startswith("cpb:r:"))
async def begin_secret_reference_edit(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, request_token = _callback_ids(
        str(callback.data), expected_parts=4
    )
    business_id = control._token_uuid(business_token)
    request_id = control._token_uuid(request_token)
    actor = await _actor(int(callback.from_user.id), business_id)
    request = await asyncio.to_thread(
        get_bot_provisioning,
        actor=actor,
        request_id=request_id,
    )
    if request.status in {
        BotProvisioningStatus.COMPLETED,
        BotProvisioningStatus.CANCELLED,
        BotProvisioningStatus.VERIFYING,
    }:
        await callback.answer("Ссылку нельзя изменить в текущем статусе.", show_alert=True)
        return
    await state.set_state(ManagedBotSetupState.token_reference)
    await state.update_data(business_id=business_id, request_id=request.id)
    await callback.answer()
    await control._callback_message(callback).answer(
        "Напишите только имя переменной, в которой хранится токен бота.\n"
        "Пример: CLIENTPLATFORM_SECRET_TELEGRAM_MY_PRACTICE"
    )


async def _store_token_reference(
    message: Message,
    state: FSMContext,
    *,
    reference: str,
) -> None:
    data = await state.get_data()
    business_id = str(data["business_id"])
    request_id = str(data["request_id"])
    actor = await _actor(control._user_id(message), business_id)
    request = await asyncio.to_thread(
        submit_botfather_secret_references,
        actor=actor,
        request_id=request_id,
        credential_reference=reference,
    )
    await state.clear()
    await message.answer(
        "Ссылка на токен сохранена. Значение токена в ClientPlatform не передавалось. "
        "Telegram webhook использоваться не будет.",
        reply_markup=_status_keyboard(business_id, request),
    )


@router.message(ManagedBotSetupState.token_reference)
async def receive_token_reference(message: Message, state: FSMContext) -> None:
    try:
        reference = _secret_reference_from_input(str(message.text or ""))
    except RawSecretInputError:
        await _delete_sensitive_message(message)
        await message.answer(
            "Похоже, Вы отправили секретное значение. Сообщение удалено.\n\n"
            "Сохраните токен в secret-store и пришлите только имя переменной "
            "CLIENTPLATFORM_SECRET_... ."
        )
        return
    except ValueError:
        await message.answer(
            "Нужно имя переменной вида CLIENTPLATFORM_SECRET_TELEGRAM_MY_PRACTICE."
        )
        return
    await _store_token_reference(message, state, reference=reference)


@router.message(ManagedBotSetupState.webhook_reference)
async def receive_webhook_reference(message: Message, state: FSMContext) -> None:
    """Recover an unfinished wizard from the removed webhook-secret step."""

    data = await state.get_data()
    reference = str(data.get("token_reference") or "")
    if not reference:
        await state.set_state(ManagedBotSetupState.token_reference)
        await message.answer(
            "Telegram переведён на polling. Отдельный webhook-секрет больше не нужен. "
            "Пришлите имя переменной с токеном бота."
        )
        return
    await _store_token_reference(message, state, reference=reference)


@router.callback_query(F.data.startswith("cpb:v:"))
async def verify_and_connect_bot(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, request_token = _callback_ids(
        str(callback.data), expected_parts=4
    )
    business_id = control._token_uuid(business_token)
    request_id = control._token_uuid(request_token)
    actor = await _actor(int(callback.from_user.id), business_id)
    await callback.answer("Проверяю подключение")
    message = control._callback_message(callback)
    await message.answer(
        "Проверяю бота в Telegram, отключаю возможный старый webhook и готовлю polling. "
        "Токен остаётся в secret-store."
    )
    try:
        completed = await finalize_botfather_provisioning(
            actor=actor,
            request_id=request_id,
        )
    except BotProvisioningVerificationFailed:
        await message.answer(
            "Telegram не подтвердил подключение. Проверьте username, переменную с "
            "токеном и доступ сервера к api.telegram.org."
        )
    except (BotProvisioningInvariantViolation, ConnectionInvariantViolation):
        await message.answer(
            "Подключение уже изменилось или у бизнеса есть другой активный бот. "
            "Обновите статус."
        )
    except (BotProvisioningNotFound, ConnectionError, TenancyError):
        await message.answer("Подключение недоступно в этом бизнесе.")
    else:
        await message.answer(
            f"Готово. @{completed.verified_username} подключён к ClientPlatform через polling."
        )
    finally:
        await state.clear()
        await _send_status(
            message,
            user_id=int(callback.from_user.id),
            business_id=business_id,
        )


@router.callback_query(F.data.startswith("cpb:c:"))
async def cancel_bot_setup(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, request_token = _callback_ids(
        str(callback.data), expected_parts=4
    )
    business_id = control._token_uuid(business_token)
    request_id = control._token_uuid(request_token)
    actor = await _actor(int(callback.from_user.id), business_id)
    try:
        await asyncio.to_thread(
            cancel_botfather_provisioning,
            actor=actor,
            request_id=request_id,
        )
    except BotProvisioningInvariantViolation:
        await callback.answer(
            "Активную проверку или готового бота отменить нельзя.",
            show_alert=True,
        )
        return
    await callback.answer("Подключение отменено")
    await state.clear()
    await _send_status(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


__all__ = [
    "ManagedBotSetupState",
    "RawSecretInputError",
    "install_dashboard_button",
    "router",
]

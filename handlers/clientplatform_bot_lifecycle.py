from __future__ import annotations

import asyncio
import importlib
from types import ModuleType

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clientplatform.application.managed_bot_owner import (
    activate_managed_bot_for_owner,
    disable_managed_bot_for_owner,
    get_managed_bot_owner_snapshot,
    revoke_managed_bot_for_owner,
)
from clientplatform.domain.bot_provisioning import (
    BotProvisioningStatus,
    ManagedBotProvisioningRequest,
)
from clientplatform.domain.connections import (
    ConnectionInvariantViolation,
    ConnectionNotFound,
    ConnectionStatus,
    ManagedBotStatus,
)
from clientplatform.domain.managed_bot_owner import (
    ManagedBotOwnerLifecycleResult,
    ManagedBotOwnerSnapshot,
    ManagedBotWebhookOperationFailed,
)
from clientplatform.domain.tenancy import TenancyError

control = importlib.import_module(".clientplatform_control", __package__)
setup = importlib.import_module(".clientplatform_bot_setup", __package__)

router = Router(name="clientplatform_bot_lifecycle")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

_STATUS_LABELS = {
    ManagedBotStatus.ACTIVE: "активен",
    ManagedBotStatus.DISABLED: "временно отключён",
    ManagedBotStatus.REVOKED: "отозван навсегда",
}
_CONNECTION_STATUS_LABELS = {
    ConnectionStatus.PENDING: "ожидает проверки",
    ConnectionStatus.ACTIVE: "активно",
    ConnectionStatus.ATTENTION: "требует внимания",
    ConnectionStatus.DISABLED: "отключено",
    ConnectionStatus.REVOKED: "отозвано",
}


def _callback_ids(data: str, *, expected_parts: int) -> list[str]:
    parts = str(data or "").split(":")
    if len(parts) != expected_parts:
        raise ValueError("invalid managed bot lifecycle callback")
    return parts


def _tokens(business_id: str, managed_bot_id: str) -> tuple[str, str]:
    return control._uuid_token(business_id), control._uuid_token(managed_bot_id)


def _ids(data: str) -> tuple[str, str]:
    _, _, business_token, bot_token = _callback_ids(data, expected_parts=4)
    return control._token_uuid(business_token), control._token_uuid(bot_token)


def _snapshot_text(snapshot: ManagedBotOwnerSnapshot) -> str:
    identity = (
        f"@{snapshot.username}"
        if snapshot.username
        else snapshot.display_name or f"bot {snapshot.external_bot_id}"
    )
    lines = [
        "Управление Telegram-ботом",
        "",
        f"Бот: {identity}.",
        f"Статус: {_STATUS_LABELS[snapshot.bot_status]}.",
        (
            "Состояние подключения: "
            f"{_CONNECTION_STATUS_LABELS[snapshot.connection_status]}."
        ),
        "Транспорт: polling. Telegram webhook не используется.",
        "",
        "Очередь этого бота:",
        f"• ожидают: {snapshot.pending_events};",
        f"• обрабатываются: {snapshot.processing_events};",
        f"• ожидают повторной попытки: {snapshot.retry_events};",
        f"• успешно обработаны: {snapshot.processed_events};",
        f"• завершены с ошибкой: {snapshot.dead_events}.",
    ]
    if snapshot.last_processed_at:
        lines.append(f"Последняя успешная обработка: {snapshot.last_processed_at}.")
    if snapshot.last_dead_at:
        lines.append(f"Последняя окончательная ошибка: {snapshot.last_dead_at}.")
    if snapshot.bot_status == ManagedBotStatus.DISABLED:
        lines.extend(
            [
                "",
                "Новые сообщения сейчас не принимаются. Включение повторно проверит "
                "бота, удалит возможный старый webhook и вернёт polling.",
            ]
        )
    elif snapshot.bot_status == ManagedBotStatus.REVOKED:
        lines.extend(
            [
                "",
                "Это подключение отозвано необратимо. Для работы нужен новый мастер "
                "подключения.",
            ]
        )
    return "\n".join(lines)


def _snapshot_keyboard(snapshot: ManagedBotOwnerSnapshot) -> InlineKeyboardMarkup:
    business_token, bot_token = _tokens(
        snapshot.business_id,
        snapshot.managed_bot_id,
    )
    rows: list[list[tuple[str, str]]] = []
    if snapshot.bot_status == ManagedBotStatus.ACTIVE:
        rows.append(
            [
                (
                    "Временно отключить",
                    f"cpbl:dc:{business_token}:{bot_token}",
                )
            ]
        )
    elif snapshot.bot_status == ManagedBotStatus.DISABLED:
        rows.append(
            [("Включить polling снова", f"cpbl:ax:{business_token}:{bot_token}")]
        )
    if snapshot.bot_status != ManagedBotStatus.REVOKED:
        rows.append(
            [("Отозвать навсегда", f"cpbl:rc:{business_token}:{bot_token}")]
        )
    rows.extend(
        [
            [("Обновить", f"cpbl:o:{business_token}:{bot_token}")],
            [("К карточке бота", f"cpb:o:{business_token}")],
        ]
    )
    return control._keyboard(rows)


def _confirmation_keyboard(
    *,
    business_id: str,
    managed_bot_id: str,
    action: str,
    label: str,
) -> InlineKeyboardMarkup:
    business_token, bot_token = _tokens(business_id, managed_bot_id)
    return control._keyboard(
        [
            [(label, f"cpbl:{action}:{business_token}:{bot_token}")],
            [("Отмена", f"cpbl:o:{business_token}:{bot_token}")],
        ]
    )


def install_lifecycle_controls(setup_module: ModuleType) -> None:
    """Add lifecycle entry point without exposing secrets to the UI layer."""

    if bool(getattr(setup_module, "_managed_bot_lifecycle_installed", False)):
        return
    original = setup_module._status_keyboard

    def status_with_lifecycle(
        business_id: str,
        request: ManagedBotProvisioningRequest | None,
    ) -> InlineKeyboardMarkup:
        markup = original(business_id, request)
        if (
            request is None
            or request.status != BotProvisioningStatus.COMPLETED
            or request.managed_bot_id is None
        ):
            return markup
        business_token, bot_token = _tokens(
            business_id,
            request.managed_bot_id,
        )
        button = InlineKeyboardButton(
            text="Управление и состояние",
            callback_data=f"cpbl:o:{business_token}:{bot_token}",
        )
        return InlineKeyboardMarkup(
            inline_keyboard=[[button], *markup.inline_keyboard]
        )

    setup_module._status_keyboard = status_with_lifecycle
    setup_module._managed_bot_lifecycle_installed = True


async def _actor(user_id: int, business_id: str):
    return await setup._actor(user_id, business_id)


async def _send_snapshot(
    message: Message,
    *,
    user_id: int,
    business_id: str,
    managed_bot_id: str,
) -> ManagedBotOwnerSnapshot:
    actor = await _actor(user_id, business_id)
    snapshot = await asyncio.to_thread(
        get_managed_bot_owner_snapshot,
        actor=actor,
        managed_bot_id=managed_bot_id,
    )
    await message.answer(
        _snapshot_text(snapshot),
        reply_markup=_snapshot_keyboard(snapshot),
    )
    return snapshot


async def _safe_send_snapshot(
    message: Message,
    *,
    user_id: int,
    business_id: str,
    managed_bot_id: str,
) -> bool:
    try:
        await _send_snapshot(
            message,
            user_id=user_id,
            business_id=business_id,
            managed_bot_id=managed_bot_id,
        )
    except (ConnectionNotFound, TenancyError):
        await message.answer(
            "Подключение больше недоступно в этом бизнесе. Обновите карточку бота."
        )
        return False
    return True


async def _report_result(message: Message, result: ManagedBotOwnerLifecycleResult) -> None:
    if result.warning_code == "webhook_detach_failed":
        await message.answer(
            "Локальный polling-маршрут уже закрыт, очередь очищена. Telegram не "
            "подтвердил удаление webhook; оператору нужно проверить отсутствие "
            "старого webhook отдельно."
        )


@router.callback_query(F.data.startswith("cpbl:o:"))
async def open_lifecycle_status(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, managed_bot_id = _ids(str(callback.data))
    await callback.answer()
    await state.clear()
    await _safe_send_snapshot(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
        managed_bot_id=managed_bot_id,
    )


@router.callback_query(F.data.startswith("cpbl:dc:"))
async def confirm_disable(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, managed_bot_id = _ids(str(callback.data))
    await callback.answer()
    await state.clear()
    await control._callback_message(callback).answer(
        "Временно отключить бота? Polling этого бота остановится, новые сообщения "
        "перестанут приниматься, а незавершённые события будут закрыты без "
        "сохранения payload.",
        reply_markup=_confirmation_keyboard(
            business_id=business_id,
            managed_bot_id=managed_bot_id,
            action="dx",
            label="Да, временно отключить",
        ),
    )


@router.callback_query(F.data.startswith("cpbl:dx:"))
async def execute_disable(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, managed_bot_id = _ids(str(callback.data))
    await callback.answer("Отключаю")
    message = control._callback_message(callback)
    try:
        actor = await _actor(int(callback.from_user.id), business_id)
        result = await disable_managed_bot_for_owner(
            actor=actor,
            managed_bot_id=managed_bot_id,
        )
    except ConnectionInvariantViolation:
        await message.answer(
            "Состояние подключения уже изменилось. Обновите карточку бота."
        )
        await state.clear()
        return
    except (ConnectionNotFound, TenancyError):
        await message.answer("Подключение недоступно в этом бизнесе.")
        await state.clear()
        return
    await state.clear()
    await message.answer("Бот временно отключён, его polling остановлен.")
    await _report_result(message, result)
    await _safe_send_snapshot(
        message,
        user_id=int(callback.from_user.id),
        business_id=business_id,
        managed_bot_id=managed_bot_id,
    )


@router.callback_query(F.data.startswith("cpbl:ax:"))
async def execute_activate(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, managed_bot_id = _ids(str(callback.data))
    await callback.answer("Проверяю и включаю")
    message = control._callback_message(callback)
    try:
        actor = await _actor(int(callback.from_user.id), business_id)
        await activate_managed_bot_for_owner(
            actor=actor,
            managed_bot_id=managed_bot_id,
        )
    except ManagedBotWebhookOperationFailed:
        await message.answer(
            "Telegram не подтвердил бота или отключение старого webhook. "
            "Локальный polling-маршрут не включён."
        )
    except ConnectionInvariantViolation:
        await message.answer(
            "В этом бизнесе уже есть другой активный Telegram-бот. "
            "Сначала отключите его."
        )
    except (ConnectionNotFound, TenancyError):
        await message.answer("Подключение недоступно в этом бизнесе.")
    else:
        await message.answer(
            "Бот снова включён. Telegram webhook отключён, polling будет "
            "подхвачен gateway-процессом."
        )
    await state.clear()
    await _safe_send_snapshot(
        message,
        user_id=int(callback.from_user.id),
        business_id=business_id,
        managed_bot_id=managed_bot_id,
    )


@router.callback_query(F.data.startswith("cpbl:rc:"))
async def confirm_revoke(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, managed_bot_id = _ids(str(callback.data))
    await callback.answer()
    await state.clear()
    await control._callback_message(callback).answer(
        "Отозвать подключение навсегда? Это необратимо: текущий polling-маршрут "
        "нельзя будет включить снова. Для повторной работы понадобится новое "
        "подключение.",
        reply_markup=_confirmation_keyboard(
            business_id=business_id,
            managed_bot_id=managed_bot_id,
            action="rx",
            label="Да, отозвать навсегда",
        ),
    )


@router.callback_query(F.data.startswith("cpbl:rx:"))
async def execute_revoke(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, managed_bot_id = _ids(str(callback.data))
    await callback.answer("Отзываю подключение")
    message = control._callback_message(callback)
    try:
        actor = await _actor(int(callback.from_user.id), business_id)
        result = await revoke_managed_bot_for_owner(
            actor=actor,
            managed_bot_id=managed_bot_id,
        )
    except ConnectionInvariantViolation:
        await message.answer(
            "Состояние подключения уже изменилось. Обновите карточку бота."
        )
        await state.clear()
        return
    except (ConnectionNotFound, TenancyError):
        await message.answer("Подключение недоступно в этом бизнесе.")
        await state.clear()
        return
    await state.clear()
    await message.answer("Подключение отозвано навсегда, polling остановлен.")
    await _report_result(message, result)
    await _safe_send_snapshot(
        message,
        user_id=int(callback.from_user.id),
        business_id=business_id,
        managed_bot_id=managed_bot_id,
    )

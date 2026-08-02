from __future__ import annotations

"""Serialize ClientPlatform interactions and keep Telegram FSM/UI transitions safe."""

import asyncio
import importlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from clientplatform.application.tenancy import list_accessible_businesses, rename_business
from clientplatform.domain.activity import BusinessProfileStatus, CapabilityStatus

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_interaction_safety")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())


class ClientPlatformSafetyState(StatesGroup):
    business_name = State()


_RECENT_ACTION_TTL_SECONDS = 4.0
_RECENT_ACTION_LIMIT = 4096
_ONE_SHOT_PREFIXES = (
    "cp:entry:",
    "cp:business:",
    "cp:clients:",
    "cp:results:",
    "cp:editact:",
    "cp:cap:",
    "cp:invite:",
    "cp:client:",
    "cp:cprograms:",
    "cp:cprog:",
    "cp:book:",
    "cp:progadd:",
    "cp:offeradd:",
    "cp:slotadd:",
    "cp:deliver:",
    "cp:sendp:",
    "cp:sendc:",
    "cpb:o:",
    "cpb:n:",
    "cpb:b:",
    "cpb:r:",
    "cpb:v:",
    "cpb:c:",
    "cps:rename:",
    "cps:cancel:",
)


def _command_like(value: str) -> bool:
    return not value.strip() or value.lstrip().startswith("/")


def _event_user_id(event: TelegramObject) -> int | None:
    user = getattr(event, "from_user", None)
    return None if user is None else int(user.id)


def _event_chat_id(event: TelegramObject) -> int:
    if isinstance(event, Message):
        return int(event.chat.id)
    if isinstance(event, CallbackQuery) and isinstance(event.message, Message):
        return int(event.message.chat.id)
    return 0


def _callback_conflicts_with_state(current_state: str | None, callback_data: str) -> bool:
    if not current_state:
        return False
    if current_state.startswith("ManagedBotSetupState:"):
        return not callback_data.startswith(("cpb:c:", "cpb:b:"))
    if current_state.startswith("ClientPlatformControlState:"):
        # Every legacy control state represents a pending text/material answer.
        return callback_data.startswith(("cp:", "cpb:", "cps:"))
    if current_state.startswith("ClientPlatformSafetyState:"):
        return not callback_data.startswith("cps:cancel:")
    # Other builders own their own cp:* callbacks, but must not be silently
    # replaced by the unrelated managed-bot wizard.
    return callback_data.startswith("cpb:")


async def _remove_source_keyboard(callback: CallbackQuery) -> None:
    if not isinstance(callback.message, Message):
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        return


class ClientPlatformInteractionSafetyMiddleware(BaseMiddleware):
    """Single-flight user actions, reject stale-flow callbacks and deduplicate taps."""

    def __init__(self) -> None:
        self._locks: dict[tuple[int, int, int], asyncio.Lock] = {}
        self._recent_actions: OrderedDict[tuple[int, int, str], float] = OrderedDict()

    def _lock_for(self, *, bot_id: int, chat_id: int, user_id: int) -> asyncio.Lock:
        key = (bot_id, chat_id, user_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _is_duplicate_action(self, *, bot_id: int, user_id: int, data: str) -> bool:
        now = time.monotonic()
        while self._recent_actions:
            _, created_at = next(iter(self._recent_actions.items()))
            if now - created_at <= _RECENT_ACTION_TTL_SECONDS:
                break
            self._recent_actions.popitem(last=False)
        key = (bot_id, user_id, data)
        previous = self._recent_actions.get(key)
        self._recent_actions[key] = now
        self._recent_actions.move_to_end(key)
        while len(self._recent_actions) > _RECENT_ACTION_LIMIT:
            self._recent_actions.popitem(last=False)
        return previous is not None and now - previous <= _RECENT_ACTION_TTL_SECONDS

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = _event_user_id(event)
        if user_id is None:
            return await handler(event, data)
        bot = data.get("bot")
        bot_id = int(getattr(bot, "id", 0) or 0)
        lock = self._lock_for(
            bot_id=bot_id,
            chat_id=_event_chat_id(event),
            user_id=user_id,
        )
        async with lock:
            if isinstance(event, CallbackQuery):
                callback_data = str(event.data or "")
                if self._is_duplicate_action(
                    bot_id=bot_id,
                    user_id=user_id,
                    data=callback_data,
                ):
                    await event.answer("Действие уже выполняется.")
                    return None
                state = data.get("state")
                current_state = (
                    await state.get_state() if isinstance(state, FSMContext) else None
                )
                if _callback_conflicts_with_state(current_state, callback_data):
                    await event.answer(
                        "Сначала завершите текущий шаг или отправьте /cancel.",
                        show_alert=True,
                    )
                    return None
                result = await handler(event, data)
                if callback_data.startswith(_ONE_SHOT_PREFIXES):
                    await _remove_source_keyboard(event)
                return result
            return await handler(event, data)


def _rename_keyboard(business_id: str) -> InlineKeyboardMarkup:
    return control._keyboard(
        [[("Отменить", f"cps:cancel:{control._uuid_token(business_id)}")]]
    )


async def _send_business_name_prompt(
    message: Message,
    *,
    state: FSMContext,
    business_id: str,
    repair: bool,
) -> None:
    await state.set_state(ClientPlatformSafetyState.business_name)
    await state.update_data(
        safety_business_id=business_id,
        safety_repair_name=repair,
    )
    prefix = (
        "Ранее команда Telegram ошибочно сохранилась как название бизнеса.\n\n"
        if repair
        else ""
    )
    await message.answer(
        prefix
        + "Напишите нормальное название Вашего дела, проекта или практики. "
        "Команды, начинающиеся с /, названием не считаются.",
        reply_markup=_rename_keyboard(business_id),
    )


@router.callback_query(F.data.startswith("cps:rename:"))
async def begin_business_rename(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = control._token_uuid(str(callback.data).split(":", 2)[2])
    await control._actor(int(callback.from_user.id), business_id)
    await callback.answer()
    await _send_business_name_prompt(
        control._callback_message(callback),
        state=state,
        business_id=business_id,
        repair=False,
    )


@router.callback_query(F.data.startswith("cps:cancel:"))
async def cancel_business_rename(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = control._token_uuid(str(callback.data).split(":", 2)[2])
    await callback.answer("Отменено")
    await state.clear()
    await control._send_dashboard(
        control._callback_message(callback),
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.message(ClientPlatformSafetyState.business_name)
async def receive_business_rename(message: Message, state: FSMContext) -> None:
    value = str(message.text or "").strip()
    if _command_like(value):
        await message.answer(
            "Название не должно быть пустым или начинаться с /. Напишите обычное название."
        )
        return
    data = await state.get_data()
    business_id = str(data["safety_business_id"])
    actor = await control._actor(control._user_id(message), business_id)
    business = await asyncio.to_thread(rename_business, actor=actor, name=value)
    await state.clear()
    await message.answer(f"Название обновлено: {business.name}")
    await control._send_dashboard(
        message,
        user_id=control._user_id(message),
        business_id=business_id,
    )


def install_interaction_safety(root_router: Router, control_module: ModuleType) -> None:
    """Install one process-wide interaction boundary and safe dashboard wrappers."""

    if bool(getattr(root_router, "_clientplatform_interaction_safety_installed", False)):
        return
    middleware = ClientPlatformInteractionSafetyMiddleware()
    root_router.message.outer_middleware(middleware)
    root_router.callback_query.outer_middleware(middleware)

    original_dashboard_keyboard = control_module._dashboard_keyboard

    def dashboard_with_rename(
        business_id: str,
        capabilities: list[object],
    ) -> InlineKeyboardMarkup:
        markup = original_dashboard_keyboard(business_id, capabilities)
        button = InlineKeyboardButton(
            text="Изменить название",
            callback_data=f"cps:rename:{control_module._uuid_token(business_id)}",
        )
        return InlineKeyboardMarkup(inline_keyboard=[*markup.inline_keyboard, [button]])

    original_send_dashboard = control_module._send_dashboard

    async def safe_send_dashboard(
        message: Message,
        *,
        user_id: int,
        business_id: str,
    ) -> None:
        actor = await control_module._actor(user_id, business_id)
        profile = await asyncio.to_thread(
            control_module.get_business_profile,
            actor=actor,
        )
        capabilities = await asyncio.to_thread(
            control_module.list_business_capabilities,
            actor=actor,
        )
        active = [
            capability
            for capability in capabilities
            if capability.status == CapabilityStatus.ACTIVE
        ]
        if profile.status != BusinessProfileStatus.READY or not active:
            await control_module._send_capability_setup(
                message,
                user_id=user_id,
                business_id=business_id,
            )
            return
        await original_send_dashboard(
            message,
            user_id=user_id,
            business_id=business_id,
        )

    original_resume_business = control_module._resume_business

    async def safe_resume_business(
        message: Message,
        *,
        user_id: int,
        business_id: str,
        state: FSMContext,
    ) -> None:
        accesses = await asyncio.to_thread(
            list_accessible_businesses,
            user_id=user_id,
        )
        access = next(
            (
                item
                for item in accesses
                if str(item.business.id) == str(business_id)
            ),
            None,
        )
        if access is not None and _command_like(str(access.business.name)):
            await _send_business_name_prompt(
                message,
                state=state,
                business_id=business_id,
                repair=True,
            )
            return
        await original_resume_business(
            message,
            user_id=user_id,
            business_id=business_id,
            state=state,
        )

    control_module._dashboard_keyboard = dashboard_with_rename
    control_module._send_dashboard = safe_send_dashboard
    control_module._resume_business = safe_resume_business
    control_module._clientplatform_interaction_safety_installed = True
    root_router._clientplatform_interaction_safety_installed = True


__all__ = [
    "ClientPlatformInteractionSafetyMiddleware",
    "ClientPlatformSafetyState",
    "install_interaction_safety",
    "router",
]

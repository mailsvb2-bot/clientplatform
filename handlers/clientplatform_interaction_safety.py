from __future__ import annotations

"""Serialize ClientPlatform interactions and keep Telegram FSM/UI transitions safe."""

import asyncio
import importlib
import logging
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

from clientplatform.application.tenancy import rename_business
from clientplatform.domain.activity import BusinessProfileStatus, CapabilityStatus

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_interaction_safety")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

log = logging.getLogger(__name__)


class ClientPlatformSafetyState(StatesGroup):
    business_name = State()


_RECENT_ACTION_TTL_SECONDS = 4.0
_RECENT_ACTION_LIMIT = 4096
_CONTROL_COMMAND_LOCK_WAIT_SECONDS = 0.25
_CONTROL_COMMANDS = frozenset({"/start", "/admin", "/mybot", "/cancel"})

# Callback namespaces are shared by several independently composed routers.  Keep
# the interaction boundary semantic: screen navigation may leave an ordinary
# text wizard, while state-local buttons are accepted only by the state that
# rendered them.  This prevents a callback namespace (for example ``cpa:``)
# from accidentally blocking its own next step.
_CLIENTPLATFORM_CALLBACK_PREFIXES = (
    "cp:",
    "cpb:",
    "cpm:",
    "cpe:",
    "cpa:",
    "cpao:",
    "cps:",
    "cpj:",
    "cpp:",
    "cpy:",
    "cpsp:",
    "cpg:",
)

# Safe screen transitions.  A user may use these to leave an ordinary wizard;
# the middleware clears stale FSM data before dispatch.  Mutating confirmation
# callbacks are intentionally excluded.
_STATE_ESCAPE_PREFIXES = (
    "cp:entry:",
    "cp:business:",
    "cp:clients:",
    "cp:results:",
    "cp:cap:",
    "cp:cprograms:",
    "cp:cprog:",
    "cp:drafts:",
    "cp:dopen:",
    "cps:programs:",
    "cps:booking:",
    "cps:advanced:",
    "cps:firstgoal:",
    "cps:firstbook:",
    "cps:firstmat:",
    "cps:firstclient:",
    "cps:cancelsetup:",
    "cps:s:",
    "cps:sw:",
    "cps:sh:",
    "cps:sf:",
    "cps:sl:",
    "cps:slv:",
    "cps:sln:",
    "cps:sla:",
    "cpj:home:",
    "cpj:services:",
    "cpj:calendar:",
    "cpj:bookings:",
    "cpj:page:",
    "cpj:promote:",
    "cpj:slot:",
    "cpj:preview:",
    "cpj:share:",
    "cpj:add:",
    "cpj:edit:",
    "cpp:stats:",
    "cpp:slot:",
    "cpb:o:",
    "cpb:b:",
    "cpa:home:",
    "cpa:promote:",
    "cpa:disconnects:",
    "cpy:a:",
    "cpsp:home:",
    "cpg:home:",
    "cpg:p:",
    "cpg:c:",
)

# Read-only/repeatable navigation must never emit "Действие уже выполняется"
# merely because the user changed period, refreshed a screen, or went back.
_REPEATABLE_NAVIGATION_PREFIXES = (
    "cp:entry:",
    "cp:business:",
    "cp:clients:",
    "cp:results:",
    "cp:cap:",
    "cp:cprograms:",
    "cp:cprog:",
    "cp:drafts:",
    "cp:dopen:",
    "cps:programs:",
    "cps:booking:",
    "cps:advanced:",
    "cps:firstgoal:",
    "cps:s:",
    "cps:sw:",
    "cps:sh:",
    "cps:sf:",
    "cps:sl:",
    "cps:slv:",
    "cpj:home:",
    "cpj:services:",
    "cpj:calendar:",
    "cpj:bookings:",
    "cpj:page:",
    "cpj:promote:",
    "cpj:slot:",
    "cpj:preview:",
    "cpj:share:",
    "cpp:stats:",
    "cpp:slot:",
    "cpb:o:",
    "cpb:b:",
    "cpa:home:",
    "cpa:promote:",
    "cpa:disconnects:",
    "cpy:a:",
    "cpsp:home:",
    "cpg:home:",
    "cpg:p:",
    "cpg:c:",
)

# These state families carry secrets, explicit money consent, or privileged
# identity changes.  Old unrelated keyboards must not silently abandon them.
_SENSITIVE_STATE_PREFIXES = (
    "ManagedBotSetupState:",
    "ExistingBotSetupState:",
    "YandexScreenCodeState:",
    "ClientPlatformSafetyState:",
    "ClientPlatformAdminState:",
    "AdSpendConsentState:",
    "ClientPlatformPartnerGrowthState:",
)

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
    "cpa:home:",
    "cpa:formats:",
    "cpa:back:",
    "cps:rename:",
    "cps:cancel:",
    "cpg:start:",
    "cpg:r:",
    "cpg:a:",
    "cpg:s:",
    "cpg:ok:",
    "cpg:no:",
)


def list_accessible_businesses(*, user_id: int):
    """Resolve through the canonical module so tests and runtime share one seam."""

    return control.list_accessible_businesses(user_id=user_id)


def _command_like(value: str) -> bool:
    return not value.strip() or value.lstrip().startswith("/")


def _message_command(event: Message) -> str | None:
    """Return a normalized Telegram command, including commands with a bot suffix."""

    text = str(event.text or "").strip()
    if not text.startswith("/"):
        return None
    token = text.split(maxsplit=1)[0]
    return token.split("@", 1)[0].casefold()


async def _safe_callback_answer(callback: CallbackQuery, *args: Any, **kwargs: Any) -> bool:
    """Acknowledge Telegram callbacks without turning expired callbacks into failures."""

    try:
        await callback.answer(*args, **kwargs)
        return True
    except TelegramAPIError:
        log.debug("ClientPlatform callback answer skipped: Telegram rejected stale callback")
        return False


class _RefCountedLock:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


class ClientPlatformInteractionMiddleware(BaseMiddleware):
    """Serialize ClientPlatform events per principal and de-duplicate mutations."""

    def __init__(self) -> None:
        self._principal_locks: dict[int, _RefCountedLock] = {}
        self._principal_locks_guard = asyncio.Lock()
        self._recent_actions: OrderedDict[str, float] = OrderedDict()

    async def _acquire_principal_lock(self, principal: int) -> _RefCountedLock:
        async with self._principal_locks_guard:
            entry = self._principal_locks.get(principal)
            if entry is None:
                entry = _RefCountedLock()
                self._principal_locks[principal] = entry
            entry.users += 1
        await entry.lock.acquire()
        return entry

    async def _release_principal_lock(self, principal: int, entry: _RefCountedLock) -> None:
        entry.lock.release()
        async with self._principal_locks_guard:
            entry.users -= 1
            if entry.users <= 0 and not entry.lock.locked():
                self._principal_locks.pop(principal, None)

    def _trim_recent_actions(self, now: float) -> None:
        cutoff = now - _RECENT_ACTION_TTL_SECONDS
        while self._recent_actions:
            _, timestamp = next(iter(self._recent_actions.items()))
            if timestamp > cutoff and len(self._recent_actions) <= _RECENT_ACTION_LIMIT:
                break
            self._recent_actions.popitem(last=False)

    def _action_key(self, event: TelegramObject) -> str | None:
        if not isinstance(event, CallbackQuery):
            return None
        data = str(event.data or "")
        if not data.startswith(_ONE_SHOT_PREFIXES):
            return None
        principal = int(event.from_user.id)
        return f"{principal}:{data}"

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery):
            callback_data = str(event.data or "")
            if not callback_data.startswith(_CLIENTPLATFORM_CALLBACK_PREFIXES):
                return await handler(event, data)
            if callback_data.startswith(_REPEATABLE_NAVIGATION_PREFIXES):
                await _safe_callback_answer(event)

        principal = int(event.from_user.id)
        lock_entry = await self._acquire_principal_lock(principal)
        try:
            if isinstance(event, CallbackQuery):
                callback_data = str(event.data or "")
                if callback_data.startswith(_REPEATABLE_NAVIGATION_PREFIXES):
                    state = data.get("state")
                    if isinstance(state, FSMContext) and callback_data.startswith(
                        _STATE_ESCAPE_PREFIXES
                    ):
                        current_state = await state.get_state()
                        if current_state and not current_state.startswith(
                            _SENSITIVE_STATE_PREFIXES
                        ):
                            await state.clear()
                    return await handler(event, data)

            action_key = self._action_key(event)
            if action_key:
                now = time.monotonic()
                self._trim_recent_actions(now)
                prior = self._recent_actions.get(action_key)
                if prior is not None and now - prior <= _RECENT_ACTION_TTL_SECONDS:
                    if isinstance(event, CallbackQuery):
                        await _safe_callback_answer(event, "Действие уже выполняется")
                    return None
                self._recent_actions[action_key] = now
                self._recent_actions.move_to_end(action_key)

            state = data.get("state")
            if isinstance(event, CallbackQuery) and isinstance(state, FSMContext):
                callback_data = str(event.data or "")
                current_state = await state.get_state()
                if current_state and callback_data.startswith(_STATE_ESCAPE_PREFIXES):
                    if not current_state.startswith(_SENSITIVE_STATE_PREFIXES):
                        await state.clear()

            return await handler(event, data)
        finally:
            await self._release_principal_lock(principal, lock_entry)


class ClientPlatformCommandLockMiddleware(BaseMiddleware):
    """Keep command recovery responsive while ordinary events are serialized."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)
        command = _message_command(event)
        if command not in _CONTROL_COMMANDS:
            return await handler(event, data)
        state = data.get("state")
        if isinstance(state, FSMContext):
            await state.clear()
        return await handler(event, data)


def install_interaction_safety(
    *,
    control_module: ModuleType,
    simple_module: ModuleType,
) -> None:
    """Install middleware once on the canonical ClientPlatform routers."""

    if bool(getattr(control_module, "_interaction_safety_installed", False)):
        return
    middleware = ClientPlatformInteractionMiddleware()
    command_middleware = ClientPlatformCommandLockMiddleware()
    simple_module.router.message.middleware(middleware)
    simple_module.router.callback_query.middleware(middleware)
    control_module.router.message.middleware(command_middleware)
    control_module.router.callback_query.middleware(middleware)
    control_module._interaction_safety_installed = True


__all__ = [
    "ClientPlatformCommandLockMiddleware",
    "ClientPlatformInteractionMiddleware",
    "ClientPlatformSafetyState",
    "install_interaction_safety",
    "router",
]

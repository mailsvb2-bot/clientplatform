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
_OWNER_NAVIGATION_PREFIXES = (
    "cpo:more:",
    "cpo:clients:",
    "cpo:content:",
    "cpo:settings:",
    "cpo:work:",
    "cpo:ads:",
)

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
    *_OWNER_NAVIGATION_PREFIXES,
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
    "cp:client:",
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
    "cps:swv:",
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
    *_OWNER_NAVIGATION_PREFIXES,
)

# Read-only/repeatable navigation must never emit "Действие уже выполняется"
# merely because the user changed period, refreshed a screen, or went back.
_REPEATABLE_NAVIGATION_PREFIXES = (
    "cp:entry:",
    "cp:business:",
    "cp:clients:",
    "cp:results:",
    "cp:cap:",
    "cp:client:",
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
    "cps:swv:",
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
    *_OWNER_NAVIGATION_PREFIXES,
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


def _event_user_id(event: TelegramObject) -> int | None:
    user = getattr(event, "from_user", None)
    return None if user is None else int(user.id)


def _event_chat_id(event: TelegramObject) -> int:
    if isinstance(event, Message):
        return int(event.chat.id)
    if isinstance(event, CallbackQuery) and isinstance(event.message, Message):
        return int(event.message.chat.id)
    return 0


def _is_clientplatform_callback(callback_data: str) -> bool:
    return callback_data.startswith(_CLIENTPLATFORM_CALLBACK_PREFIXES)


def _is_admin_screen_navigation(callback_data: str) -> bool:
    """Recognize only non-mutating token-first admin navigation callbacks."""

    parts = callback_data.split(":")
    if len(parts) < 3 or parts[0] != "cpa":
        return False
    # Advertising callbacks are action-first and are handled separately.
    if parts[1] in {
        "home",
        "connect",
        "yandex-cancel",
        "promote",
        "slot",
        "conn",
        "campaign",
        "confirm",
        "disconnects",
        "disconnect",
        "revoke",
        "formats",
        "back",
    }:
        return False
    return parts[2] in {"menu", "back", "leave"}


def _is_state_escape_callback(callback_data: str) -> bool:
    return callback_data.startswith(_STATE_ESCAPE_PREFIXES) or _is_admin_screen_navigation(
        callback_data
    )


def _is_repeatable_navigation(callback_data: str) -> bool:
    return callback_data.startswith(
        _REPEATABLE_NAVIGATION_PREFIXES
    ) or _is_admin_screen_navigation(callback_data)


def _state_local_callback_allowed(current_state: str, callback_data: str) -> bool:
    """Return True only for callbacks that belong to the active FSM step."""

    if current_state.startswith("ManagedBotSetupState:"):
        return callback_data.startswith(("cpb:c:", "cpb:b:"))
    if current_state.startswith("ClientPlatformSafetyState:"):
        return callback_data.startswith("cps:cancel:")
    if current_state.startswith("YandexScreenCodeState:waiting_code"):
        return callback_data.startswith("cpa:yandex-cancel:")
    if current_state.startswith("AdConnectionState:selecting_connection"):
        return callback_data.startswith("cpa:conn:")
    if current_state.startswith("AdConnectionState:selecting_campaign"):
        return callback_data.startswith("cpa:campaign:")
    if current_state.startswith("AdConnectionState:confirming_publication"):
        return callback_data == "cpa:confirm"
    if current_state.startswith("ClientPlatformControlState:booking_start"):
        return callback_data.startswith("cpj:wizcancel:")
    if current_state.startswith("ClientPlatformControlState:booking_duration"):
        return callback_data.startswith(
            (
                "cpj:wizdur:",
                "cpj:wizcustom:",
                "cpj:wizback:",
                "cpj:wizcancel:",
            )
        )
    if current_state.startswith("ClientPlatformProgramBuilderState:review"):
        return callback_data.startswith(("cp:dadd:", "cp:dpub:", "cp:darc:"))
    if current_state.startswith("AdSpendConsentState:confirming_consent"):
        return callback_data.startswith("cpsp:confirm:")
    if current_state.startswith("ClientPlatformAdminOpsState:"):
        return callback_data.startswith("cpao:return-")
    return False


def _callback_can_escape_state(current_state: str, callback_data: str) -> bool:
    if not _is_state_escape_callback(callback_data):
        return False
    # The spend-consent screen has an explicit "Отмена" / home button.  It is
    # safe because the target handler rebuilds the screen and no provider
    # mutation has happened yet.
    if current_state.startswith("AdSpendConsentState:"):
        return callback_data.startswith("cpsp:home:")
    if current_state.startswith(_SENSITIVE_STATE_PREFIXES):
        return False
    return True


def _callback_should_clear_state(
    current_state: str | None,
    callback_data: str,
) -> bool:
    if not current_state:
        return False
    if _state_local_callback_allowed(current_state, callback_data):
        return False
    return _callback_can_escape_state(current_state, callback_data)


def _callback_conflicts_with_state(current_state: str | None, callback_data: str) -> bool:
    if not current_state or not _is_clientplatform_callback(callback_data):
        return False
    if _state_local_callback_allowed(current_state, callback_data):
        return False
    if _callback_can_escape_state(current_state, callback_data):
        return False
    return True


async def _remove_source_keyboard(callback: CallbackQuery) -> None:
    if not isinstance(callback.message, Message):
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        return


async def _answer_callback(
    callback: CallbackQuery,
    text: str | None = None,
    *,
    show_alert: bool = False,
) -> None:
    """Stop Telegram's spinner without letting an expired callback break the flow."""

    try:
        await callback.answer(text=text, show_alert=show_alert)
    except TelegramAPIError:
        return


class ClientPlatformInteractionSafetyMiddleware(BaseMiddleware):
    """Single-flight user actions, reject stale-flow callbacks and deduplicate taps."""

    def __init__(self) -> None:
        self._locks: dict[tuple[int, int, int], asyncio.Lock] = {}
        self._lock_users: dict[tuple[int, int, int], int] = {}
        self._recent_actions: OrderedDict[tuple[int, int, str], float] = OrderedDict()

    def _lock_for(
        self,
        *,
        bot_id: int,
        chat_id: int,
        user_id: int,
    ) -> tuple[tuple[int, int, int], asyncio.Lock]:
        key = (bot_id, chat_id, user_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        self._lock_users[key] = self._lock_users.get(key, 0) + 1
        return key, lock

    def _release_lock_reference(
        self,
        key: tuple[int, int, int],
        lock: asyncio.Lock,
    ) -> None:
        remaining = self._lock_users.get(key, 1) - 1
        if remaining > 0:
            self._lock_users[key] = remaining
            return
        self._lock_users.pop(key, None)
        if not lock.locked() and self._locks.get(key) is lock:
            self._locks.pop(key, None)

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

    async def _run_control_command(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
        *,
        lock: asyncio.Lock,
        command: str,
        bot_id: int,
        user_id: int,
    ) -> Any:
        """Prefer serialization, but never let a stale action make recovery commands silent."""

        acquired = False
        try:
            await asyncio.wait_for(
                lock.acquire(),
                timeout=_CONTROL_COMMAND_LOCK_WAIT_SECONDS,
            )
            acquired = True
        except TimeoutError:
            log.warning(
                "Bypassing busy ClientPlatform interaction lock command=%s bot_id=%s user_id=%s",
                command,
                bot_id,
                user_id,
            )
        try:
            return await handler(event, data)
        finally:
            if acquired:
                lock.release()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = _event_user_id(event)
        if user_id is None:
            return await handler(event, data)

        callback_data: str | None = None
        if isinstance(event, CallbackQuery):
            callback_data = str(event.data or "")
            # This middleware is mounted outside router filters. Foreign callback
            # namespaces must propagate untouched to payments/settings/other
            # routers: no dedup record, no per-principal lock, no eager answer.
            if not _is_clientplatform_callback(callback_data):
                return await handler(event, data)

        bot = data.get("bot")
        bot_id = int(getattr(bot, "id", 0) or 0)
        lock_key, lock = self._lock_for(
            bot_id=bot_id,
            chat_id=_event_chat_id(event),
            user_id=user_id,
        )
        try:
            if isinstance(event, CallbackQuery):
                assert callback_data is not None
                repeatable_navigation = _is_repeatable_navigation(callback_data)
                if (
                    not repeatable_navigation
                    and self._is_duplicate_action(
                        bot_id=bot_id,
                        user_id=user_id,
                        data=callback_data,
                    )
                ):
                    await _answer_callback(event, "Действие уже выполняется.")
                    return None
                state = data.get("state")
                current_state = (
                    await state.get_state() if isinstance(state, FSMContext) else None
                )
                if _callback_conflicts_with_state(current_state, callback_data):
                    await _answer_callback(
                        event,
                        "Сначала завершите текущий шаг или отправьте /cancel.",
                        show_alert=True,
                    )
                    return None

                # Read-only/repeatable navigation gets an immediate spinner ack.
                # Mutating or validated actions intentionally do not: their
                # handler owns the first answer so a semantic toast/alert cannot
                # be consumed by a preceding blank answerCallbackQuery call.
                eager_ack = repeatable_navigation
                if eager_ack:
                    await _answer_callback(event)

                async with lock:
                    current_state = (
                        await state.get_state() if isinstance(state, FSMContext) else None
                    )
                    if _callback_conflicts_with_state(current_state, callback_data):
                        if eager_ack and isinstance(event.message, Message):
                            await event.message.answer(
                                "Сначала завершите текущий шаг или отправьте /cancel."
                            )
                        else:
                            await _answer_callback(
                                event,
                                "Сначала завершите текущий шаг или отправьте /cancel.",
                                show_alert=True,
                            )
                        return None
                    if (
                        isinstance(state, FSMContext)
                        and _callback_should_clear_state(current_state, callback_data)
                    ):
                        await state.clear()
                    try:
                        result = await handler(event, data)
                    finally:
                        if not eager_ack:
                            # If the handler already sent a semantic answer this
                            # becomes a harmless expired/duplicate blank ack and
                            # _answer_callback intentionally swallows Telegram's
                            # API error. If it did not, this closes the spinner.
                            await _answer_callback(event)
                    if callback_data.startswith(_ONE_SHOT_PREFIXES):
                        await _remove_source_keyboard(event)
                    return result
            if isinstance(event, Message):
                command = _message_command(event)
                if command in _CONTROL_COMMANDS:
                    return await self._run_control_command(
                        handler,
                        event,
                        data,
                        lock=lock,
                        command=command,
                        bot_id=bot_id,
                        user_id=user_id,
                    )
            async with lock:
                return await handler(event, data)
        finally:
            self._release_lock_reference(lock_key, lock)


def _rename_keyboard(business_id: str) -> InlineKeyboardMarkup:
    try:
        business_token = control._uuid_token(business_id)
    except (TypeError, ValueError):
        return InlineKeyboardMarkup(inline_keyboard=[])
    return control._keyboard(
        [[("Отменить", f"cps:cancel:{business_token}")]]
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
    await _answer_callback(callback)
    await _send_business_name_prompt(
        control._callback_message(callback),
        state=state,
        business_id=business_id,
        repair=False,
    )


@router.callback_query(F.data.startswith("cps:cancel:"))
async def cancel_business_rename(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = control._token_uuid(str(callback.data).split(":", 2)[2])
    await _answer_callback(callback, "Отменено")
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
    """Install one interaction boundary and a low-round-trip production dashboard."""

    if bool(getattr(root_router, "_clientplatform_interaction_safety_installed", False)):
        return
    middleware = ClientPlatformInteractionSafetyMiddleware()
    root_router.message.outer_middleware(middleware)
    root_router.callback_query.outer_middleware(middleware)

    original_dashboard_keyboard = control_module._dashboard_keyboard
    original_send_dashboard = control_module._send_dashboard
    original_resume_business = control_module._resume_business
    legacy_test_double = not all(
        hasattr(control_module, attribute)
        for attribute in ("ActivityNotFound", "ClientPlatformControlState")
    )

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

    async def legacy_send_dashboard(
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
        profile_status = getattr(
            profile,
            "status",
            BusinessProfileStatus.READY,
        )
        if profile_status != BusinessProfileStatus.READY or not active:
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

    async def load_dashboard_context(
        *,
        user_id: int,
        business_id: str,
    ) -> tuple[object, object, list[object]]:
        actor = await control_module._actor(user_id, business_id)
        profile, capabilities, accesses = await asyncio.gather(
            asyncio.to_thread(control_module.get_business_profile, actor=actor),
            asyncio.to_thread(control_module.list_business_capabilities, actor=actor),
            asyncio.to_thread(list_accessible_businesses, user_id=user_id),
        )
        access = next(
            item
            for item in accesses
            if str(item.business.id) == str(business_id)
        )
        return access, profile, list(capabilities)

    async def render_dashboard(
        message: Message,
        *,
        user_id: int,
        business_id: str,
        access: object,
        profile: object,
        capabilities: list[object],
    ) -> None:
        active = [
            capability
            for capability in capabilities
            if capability.status == CapabilityStatus.ACTIVE
        ]
        profile_status = getattr(
            profile,
            "status",
            BusinessProfileStatus.READY,
        )
        if profile_status != BusinessProfileStatus.READY or not active:
            await control_module._send_capability_setup(
                message,
                user_id=user_id,
                business_id=business_id,
            )
            return
        module_lines = "\n".join(f"• {item.title}" for item in active)
        await message.answer(
            f"{access.business.name}\n\n"
            f"Чем Вы занимаетесь:\n{profile.activity_description}\n\n"
            f"Подключено:\n{module_lines}\n\n"
            "Выберите результат, который нужен сейчас.",
            reply_markup=control_module._dashboard_keyboard(
                business_id,
                active,
            ),
        )

    async def safe_send_dashboard(
        message: Message,
        *,
        user_id: int,
        business_id: str,
    ) -> None:
        if legacy_test_double:
            await legacy_send_dashboard(
                message,
                user_id=user_id,
                business_id=business_id,
            )
            return
        access, profile, capabilities = await load_dashboard_context(
            user_id=user_id,
            business_id=business_id,
        )
        await render_dashboard(
            message,
            user_id=user_id,
            business_id=business_id,
            access=access,
            profile=profile,
            capabilities=capabilities,
        )

    async def safe_resume_business(
        message: Message,
        *,
        user_id: int,
        business_id: str,
        state: FSMContext,
    ) -> None:
        if legacy_test_double:
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
            return
        try:
            access, profile, capabilities = await load_dashboard_context(
                user_id=user_id,
                business_id=business_id,
            )
        except control_module.ActivityNotFound:
            await state.set_state(control_module.ClientPlatformControlState.activity_description)
            await state.update_data(business_id=business_id, editing_activity=False)
            await message.answer(
                "Расскажите своими словами, чем Вы занимаетесь и чем помогаете клиентам.\n\n"
                "Например: «Консультирую родителей по вопросам сна детей» или "
                "«Ремонтирую автомобили и принимаю заказы на обслуживание»."
            )
            return
        if _command_like(str(access.business.name)):
            await _send_business_name_prompt(
                message,
                state=state,
                business_id=business_id,
                repair=True,
            )
            return
        await state.clear()
        current_dashboard = control_module._send_dashboard
        if current_dashboard is not safe_send_dashboard:
            await current_dashboard(
                message,
                user_id=user_id,
                business_id=business_id,
            )
            return
        await render_dashboard(
            message,
            user_id=user_id,
            business_id=business_id,
            access=access,
            profile=profile,
            capabilities=capabilities,
        )

    control_module._dashboard_keyboard = dashboard_with_rename
    control_module._send_dashboard = safe_send_dashboard
    control_module._resume_business = safe_resume_business
    control_module._clientplatform_interaction_safety_installed = True
    control_module._optimized_dashboard_queries_installed = not legacy_test_double
    root_router._clientplatform_interaction_safety_installed = True

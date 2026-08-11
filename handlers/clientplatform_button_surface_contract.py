from __future__ import annotations

"""Compose callback namespaces owned by optional ClientPlatform UI modules.

The core interaction-safety middleware is installed before lesson/media,
managed-bot lifecycle and admin-operation extensions are composed. Those
extensions still need to participate in the same FSM/navigation contract
instead of bypassing it merely because their callback shapes are added later.
"""

from types import ModuleType
from typing import Any, Awaitable, Callable, cast

from aiogram.fsm.context import FSMContext


class _SupersededInteraction(RuntimeError):
    """Internal signal that a newer recovery command owns this FSM now."""


class _GenerationBoundFSMContext(FSMContext):
    """FSM view that rejects writes after a newer recovery generation wins."""

    def __init__(
        self,
        state: FSMContext,
        generations: dict[tuple[int, int, int], int],
        principal_key: tuple[int, int, int],
        generation: int,
    ) -> None:
        self.storage = state.storage
        self.key = state.key
        self._state = state
        self._generations = generations
        self._principal_key = principal_key
        self._generation = generation

    def assert_current(self) -> None:
        if self._generations.get(self._principal_key, 0) != self._generation:
            raise _SupersededInteraction("interaction was superseded by recovery")

    async def set_state(self, state: Any = None) -> None:
        self.assert_current()
        await self._state.set_state(state)

    async def get_state(self) -> str | None:
        return await self._state.get_state()

    async def set_data(self, data: dict[str, Any]) -> None:
        self.assert_current()
        await self._state.set_data(data)

    async def get_data(self) -> dict[str, Any]:
        return await self._state.get_data()

    async def update_data(
        self,
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.assert_current()
        if data is None:
            return await self._state.update_data(**kwargs)
        return await self._state.update_data(data, **kwargs)

    async def clear(self) -> None:
        self.assert_current()
        await self._state.clear()

    async def get_value(self, key: str, default: Any = None) -> Any:
        getter = getattr(self._state, "get_value", None)
        if callable(getter):
            return await getter(key, default)
        return (await self._state.get_data()).get(key, default)

    async def set_value(self, key: str, value: Any) -> None:
        self.assert_current()
        setter = getattr(self._state, "set_value", None)
        if callable(setter):
            await setter(key, value)
            return
        await self._state.update_data(**{key: value})


def _extend_tuple(module: ModuleType, name: str, *values: str) -> None:
    current = tuple(getattr(module, name))
    setattr(module, name, tuple(dict.fromkeys((*current, *values))))


def _is_admin_ops_return(callback_data: str) -> bool:
    parts = callback_data.split(":", 3)
    return (
        len(parts) >= 3
        and parts[0] == "cpao"
        and bool(parts[1])
        and parts[2].startswith("return-")
    )


def _is_admin_stack_back(callback_data: str) -> bool:
    """Recognize token-first admin back actions that mutate navigation history."""

    parts = callback_data.split(":", 3)
    return (
        len(parts) >= 3
        and parts[0] == "cpa"
        and bool(parts[1])
        and parts[2] == "back"
    )


def _principal_key(
    safety: ModuleType,
    event: Any,
    data: dict[str, Any],
) -> tuple[int, int, int] | None:
    user_id = safety._event_user_id(event)
    if user_id is None:
        return None
    bot = data.get("bot")
    bot_id = int(getattr(bot, "id", 0) or 0)
    return (bot_id, safety._event_chat_id(event), int(user_id))


def _is_recovery_command(safety: ModuleType, event: Any) -> bool:
    if not isinstance(event, safety.Message):
        return False
    return safety._message_command(event) in {"/cancel", "/start"}


def install_button_surface_contract(safety: ModuleType) -> None:
    if bool(getattr(safety, "_button_surface_contract_installed", False)):
        return

    _extend_tuple(
        safety,
        "_CLIENTPLATFORM_CALLBACK_PREFIXES",
        "cpbl:",
        "cpcm:",
        "cpg:",
        "cpo:",
    )
    _extend_tuple(
        safety,
        "_STATE_ESCAPE_PREFIXES",
        "cp:slotadd:",
        "cp:dless:",
        "cp:dled:",
        "cp:dlcancel:",
        "cpbl:o:",
        "cpbl:dc:",
        "cpbl:rc:",
        "cpg:home:",
        "cpg:p:",
        "cpg:c:",
        "cpg:materials:",
        "cpg:mc:",
        "cpg:l:",
        "cpo:start:",
        "cpo:more:",
        "cpo:work:",
        "cpo:ads:",
    )
    _extend_tuple(
        safety,
        "_REPEATABLE_NAVIGATION_PREFIXES",
        "cp:dless:",
        "cp:dled:",
        "cp:dlcancel:",
        "cpbl:o:",
        "cpg:home:",
        "cpg:p:",
        "cpg:c:",
        "cpg:materials:",
        "cpg:mc:",
        "cpg:l:",
        "cpo:more:",
        "cpo:work:",
        "cpo:ads:",
    )
    _extend_tuple(
        safety,
        "_SENSITIVE_STATE_PREFIXES",
        "ClientPlatformPartnerGrowthState:",
    )
    _extend_tuple(
        safety,
        "_ONE_SHOT_PREFIXES",
        "cpg:start:",
        "cpg:r:",
        "cpg:a:",
        "cpg:s:",
        "cpg:sc:",
        "cpg:ok:",
        "cpg:no:",
        "cpg:b:",
    )

    original_state_local = cast(
        Callable[[str, str], bool],
        getattr(safety, "_state_local_callback_allowed"),
    )
    original_repeatable = cast(
        Callable[[str], bool],
        getattr(safety, "_is_repeatable_navigation"),
    )
    original_call = getattr(safety.ClientPlatformInteractionSafetyMiddleware, "__call__")

    def state_local_callback_allowed(current_state: str, callback_data: str) -> bool:
        if current_state.startswith("ClientPlatformProgramBuilderState:review"):
            if callback_data.startswith("cp:dless:"):
                return True
        if current_state.startswith("ClientPlatformDraftLessonEditorState:"):
            if callback_data.startswith("cp:dlcancel:"):
                return True
        if current_state.startswith("ClientPlatformCloudMediaState:choose_kind"):
            if callback_data.startswith("cpcm:k:"):
                return True
        if current_state.startswith("ClientPlatformCloudMediaState:choose_source"):
            if callback_data.startswith("cpcm:s:"):
                return True
        if current_state.startswith("ClientPlatformAdminOpsState:"):
            if _is_admin_ops_return(callback_data):
                return True
        if current_state.startswith("OneClickOwnerState:selecting_connection"):
            if callback_data.startswith("cpo:connection:"):
                return True
        if current_state.startswith("OneClickOwnerState:selecting_campaign"):
            if callback_data.startswith("cpo:campaign:"):
                return True
        if current_state.startswith("OneClickOwnerState:waiting_region"):
            if callback_data.startswith("cpo:region:"):
                return True
        return original_state_local(current_state, callback_data)

    def repeatable_navigation(callback_data: str) -> bool:
        if _is_admin_stack_back(callback_data):
            return False
        return original_repeatable(callback_data)

    async def generation_bound_call(
        self: Any,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, safety.CallbackQuery):
            callback_data = str(event.data or "")
            if not safety._is_clientplatform_callback(callback_data):
                return await original_call(self, handler, event, data)

        principal_key = _principal_key(safety, event, data)
        if principal_key is None:
            return await original_call(self, handler, event, data)

        state = data.get("state")
        if not isinstance(state, FSMContext):
            return await original_call(self, handler, event, data)

        generations = getattr(self, "_clientplatform_recovery_generations", None)
        if generations is None:
            generations = {}
            setattr(self, "_clientplatform_recovery_generations", generations)
        generation_users = getattr(
            self,
            "_clientplatform_recovery_generation_users",
            None,
        )
        if generation_users is None:
            generation_users = {}
            setattr(
                self,
                "_clientplatform_recovery_generation_users",
                generation_users,
            )
        generation_users[principal_key] = generation_users.get(principal_key, 0) + 1

        if _is_recovery_command(safety, event):
            generations[principal_key] = generations.get(principal_key, 0) + 1
        generation = generations.get(principal_key, 0)

        guarded_state = _GenerationBoundFSMContext(
            state,
            generations,
            principal_key,
            generation,
        )
        guarded_data = dict(data)
        guarded_data["state"] = guarded_state

        async def guarded_handler(
            guarded_event: Any,
            handler_data: dict[str, Any],
        ) -> Any:
            guarded_state.assert_current()
            return await handler(guarded_event, handler_data)

        try:
            try:
                return await original_call(
                    self,
                    guarded_handler,
                    event,
                    guarded_data,
                )
            except _SupersededInteraction:
                return None
        finally:
            remaining = generation_users.get(principal_key, 1) - 1
            if remaining > 0:
                generation_users[principal_key] = remaining
            else:
                generation_users.pop(principal_key, None)
                generations.pop(principal_key, None)

    setattr(safety, "_state_local_callback_allowed", state_local_callback_allowed)
    setattr(safety, "_is_repeatable_navigation", repeatable_navigation)
    setattr(
        safety.ClientPlatformInteractionSafetyMiddleware,
        "__call__",
        generation_bound_call,
    )
    setattr(safety, "_button_surface_contract_installed", True)


__all__ = ["install_button_surface_contract"]
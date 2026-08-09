from __future__ import annotations

"""Compose callback namespaces owned by optional ClientPlatform UI modules.

The core interaction-safety middleware is installed before lesson/media,
managed-bot lifecycle and admin-operation extensions are composed. Those
extensions still need to participate in the same FSM/navigation contract
instead of bypassing it merely because their callback shapes are added later.
"""

from types import ModuleType
from typing import Callable, cast


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


def install_button_surface_contract(safety: ModuleType) -> None:
    if bool(getattr(safety, "_button_surface_contract_installed", False)):
        return

    _extend_tuple(
        safety,
        "_CLIENTPLATFORM_CALLBACK_PREFIXES",
        "cpbl:",
        "cpcm:",
    )
    _extend_tuple(
        safety,
        "_STATE_ESCAPE_PREFIXES",
        "cp:dless:",
        "cp:dled:",
        "cp:dlcancel:",
        "cpbl:o:",
        "cpbl:dc:",
        "cpbl:rc:",
    )
    _extend_tuple(
        safety,
        "_REPEATABLE_NAVIGATION_PREFIXES",
        "cp:dless:",
        "cp:dled:",
        "cp:dlcancel:",
        "cpbl:o:",
    )

    original_state_local = cast(
        Callable[[str, str], bool],
        getattr(safety, "_state_local_callback_allowed"),
    )
    original_repeatable = cast(
        Callable[[str], bool],
        getattr(safety, "_is_repeatable_navigation"),
    )

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
        return original_state_local(current_state, callback_data)

    def repeatable_navigation(callback_data: str) -> bool:
        # Token-first admin ``back`` pops cp_admin_history and is therefore not
        # idempotent. Keep it available as a state-escape action, but let the
        # core middleware's duplicate-action guard reject rapid double taps.
        if _is_admin_stack_back(callback_data):
            return False
        return original_repeatable(callback_data)

    setattr(safety, "_state_local_callback_allowed", state_local_callback_allowed)
    setattr(safety, "_is_repeatable_navigation", repeatable_navigation)
    setattr(safety, "_button_surface_contract_installed", True)


__all__ = ["install_button_surface_contract"]

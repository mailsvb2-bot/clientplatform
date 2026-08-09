from __future__ import annotations

"""Compose callback namespaces owned by optional ClientPlatform UI modules.

The core interaction-safety middleware is installed before lesson/media and
managed-bot lifecycle extensions are composed. Those extensions still need to
participate in the same FSM/navigation contract instead of bypassing it merely
because their callback prefixes were added later.
"""

from types import ModuleType
from typing import Callable, cast


_INSTALLED = False


def _extend_tuple(module: ModuleType, name: str, *values: str) -> None:
    current = tuple(getattr(module, name))
    setattr(module, name, tuple(dict.fromkeys((*current, *values))))


def install_button_surface_contract(safety: ModuleType) -> None:
    global _INSTALLED
    if _INSTALLED:
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

    original = cast(
        Callable[[str, str], bool],
        getattr(safety, "_state_local_callback_allowed"),
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
        return original(current_state, callback_data)

    setattr(safety, "_state_local_callback_allowed", state_local_callback_allowed)
    _INSTALLED = True


__all__ = ["install_button_surface_contract"]

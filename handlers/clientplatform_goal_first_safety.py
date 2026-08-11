from __future__ import annotations

from types import ModuleType
from typing import Callable, cast


def _extend_tuple(module: ModuleType, name: str, *values: str) -> None:
    current = tuple(getattr(module, name))
    setattr(module, name, tuple(dict.fromkeys((*current, *values))))


def install_goal_first_safety(safety: ModuleType) -> None:
    if bool(getattr(safety, "_goal_first_safety_installed", False)):
        return

    _extend_tuple(
        safety,
        "_SENSITIVE_STATE_PREFIXES",
        "GoalFirstAutopilotState:",
    )
    _extend_tuple(
        safety,
        "_ONE_SHOT_PREFIXES",
        "cpo:gen:",
        "cpo:launch:",
        "cpo:launch-confirm:",
        "cpo:custom-clear:",
    )
    _extend_tuple(
        safety,
        "_REPEATABLE_NAVIGATION_PREFIXES",
        "cpo:custom:",
        "cpo:custom-text:",
        "cpo:custom-image:",
        "cpo:custom-video:",
        "cpo:custom-done:",
        "cpo:genask:",
        "cpo:gencheck:",
    )

    original_state_local = cast(
        Callable[[str, str], bool],
        getattr(safety, "_state_local_callback_allowed"),
    )
    original_escape = cast(
        Callable[[str, str], bool],
        getattr(safety, "_callback_can_escape_state"),
    )

    def state_local_callback_allowed(current_state: str, callback_data: str) -> bool:
        if current_state.startswith("GoalFirstAutopilotState:ready"):
            return callback_data.startswith(("cpo:custom:", "cpo:launch:"))
        if current_state.startswith("GoalFirstAutopilotState:customizing"):
            return callback_data.startswith(
                (
                    "cpo:custom:",
                    "cpo:custom-text:",
                    "cpo:custom-image:",
                    "cpo:custom-video:",
                    "cpo:custom-clear:",
                    "cpo:custom-done:",
                    "cpo:genask:",
                    "cpo:ads:",
                    "cpo:launch:",
                )
            )
        if current_state.startswith("GoalFirstAutopilotState:confirming_generation"):
            return callback_data.startswith(("cpo:gen:", "cpo:custom:"))
        if current_state.startswith("GoalFirstAutopilotState:generation_pending"):
            return callback_data.startswith("cpo:gencheck:")
        if current_state.startswith("GoalFirstAutopilotState:confirming_launch"):
            return callback_data.startswith("cpo:launch-confirm:")
        return original_state_local(current_state, callback_data)

    def callback_can_escape_state(current_state: str, callback_data: str) -> bool:
        if current_state.startswith("GoalFirstAutopilotState:"):
            if callback_data.startswith(("cpj:home:", "cpo:start:")):
                return True
        return original_escape(current_state, callback_data)

    setattr(safety, "_state_local_callback_allowed", state_local_callback_allowed)
    setattr(safety, "_callback_can_escape_state", callback_can_escape_state)
    setattr(safety, "_goal_first_safety_installed", True)


__all__ = ["install_goal_first_safety"]

from __future__ import annotations

from handlers import clientplatform_creative_studio as creative
from handlers import clientplatform_interaction_safety as safety


def test_creative_scene_actions_stay_inside_active_style_step() -> None:
    creative.install_creative_studio_safety(safety)
    state = "ClientPlatformCreativeStudioState:choosing_style"

    for callback in (
        "cpc:sv:show:business-token",
        "cpc:sv:auto:business-token",
        "cpc:sv:pick:variant:business-token",
        "cpc:sv:add:variant:business-token",
        "cpc:st:go:business-token",
    ):
        assert safety._is_clientplatform_callback(callback) is True
        assert safety._state_local_callback_allowed(state, callback) is True
        assert safety._callback_should_clear_state(state, callback) is False
        assert safety._callback_conflicts_with_state(state, callback) is False

    assert safety._is_repeatable_navigation("cpc:sv:show:business-token") is True
    assert (
        safety._callback_conflicts_with_state(
            "ClientPlatformControlState:activity_description",
            "cpc:sv:show:business-token",
        )
        is True
    )

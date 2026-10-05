from __future__ import annotations

from handlers import clientplatform_interaction_safety as safety


def test_creative_scene_actions_stay_inside_active_style_step() -> None:
    state = "ClientPlatformCreativeStudioState:choosing_style"

    for callback in (
        "cpc:sv:show:business-token",
        "cpc:st:go:business-token",
    ):
        assert safety._is_clientplatform_callback(callback) is True
        assert safety._state_local_callback_allowed(state, callback) is True
        assert safety._callback_should_clear_state(state, callback) is False
        assert safety._callback_conflicts_with_state(state, callback) is False

    assert (
        safety._callback_conflicts_with_state(
            "ClientPlatformControlState:activity_description",
            "cpc:sv:show:business-token",
        )
        is True
    )

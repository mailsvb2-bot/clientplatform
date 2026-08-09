from __future__ import annotations

from handlers import clientplatform_control as _control  # noqa: F401
from handlers.clientplatform_interaction_safety import (
    _callback_conflicts_with_state,
    _callback_should_clear_state,
    _is_repeatable_navigation,
)


def test_program_review_can_open_lesson_list() -> None:
    state_name = "ClientPlatformProgramBuilderState:review"
    data = "cp:dless:business:program:0"
    assert not _callback_conflicts_with_state(state_name, data)
    assert not _callback_should_clear_state(state_name, data)


def test_lesson_editor_cancel_remains_a_state_local_button() -> None:
    for state_name in (
        "ClientPlatformDraftLessonEditorState:title",
        "ClientPlatformDraftLessonEditorState:content",
    ):
        data = "cp:dlcancel:business:lesson"
        assert not _callback_conflicts_with_state(state_name, data)
        assert not _callback_should_clear_state(state_name, data)


def test_cloud_material_picker_buttons_belong_to_their_exact_steps() -> None:
    assert not _callback_conflicts_with_state(
        "ClientPlatformCloudMediaState:choose_kind",
        "cpcm:k:video",
    )
    assert _callback_conflicts_with_state(
        "ClientPlatformCloudMediaState:choose_kind",
        "cpcm:s:cloud",
    )
    assert not _callback_conflicts_with_state(
        "ClientPlatformCloudMediaState:choose_source",
        "cpcm:s:cloud",
    )
    assert _callback_conflicts_with_state(
        "ClientPlatformCloudMediaState:choose_source",
        "cpcm:k:video",
    )


def test_cloud_picker_can_return_to_draft_or_lesson_without_stale_state() -> None:
    for data in (
        "cp:dopen:business:program",
        "cp:dlcancel:business:lesson",
    ):
        assert not _callback_conflicts_with_state(
            "ClientPlatformCloudMediaState:choose_kind",
            data,
        )
        assert _callback_should_clear_state(
            "ClientPlatformCloudMediaState:choose_kind",
            data,
        )


def test_managed_bot_lifecycle_refresh_is_repeatable_navigation() -> None:
    data = "cpbl:o:business:bot"
    assert _is_repeatable_navigation(data)
    assert not _callback_conflicts_with_state(
        "ClientPlatformControlState:activity_description",
        data,
    )
    assert _callback_should_clear_state(
        "ClientPlatformControlState:activity_description",
        data,
    )


def test_managed_bot_lifecycle_mutation_is_not_repeatable_navigation() -> None:
    assert not _is_repeatable_navigation("cpbl:dx:business:bot")
    assert not _is_repeatable_navigation("cpbl:ax:business:bot")
    assert not _is_repeatable_navigation("cpbl:rx:business:bot")

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application import native_member_interactions as ui
from clientplatform.application.owner_input import resolve_owner_input
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.owner_input import OwnerInputSession
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.presentation.event_ui import EventHubAction


def _actor() -> TenantContext:
    return TenantContext(
        business_id=str(uuid4()),
        membership_id=str(uuid4()),
        user_id=101,
        role=PlatformRole.OWNER,
    )


def _session(position: int, *, join_url: str, provider_key: str, provider_label: str):
    return SimpleNamespace(
        position=position,
        starts_at=datetime(2026, 10, position, 17, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 10, position, 18, 0, tzinfo=timezone.utc),
        join_url=join_url,
        provider_key=provider_key,
        provider_label=provider_label,
    )


def _commands(message) -> list[str]:
    return [button.command for row in message.rows for button in row]


def test_native_event_hub_keeps_schedule_edit_action() -> None:
    actor = _actor()
    snapshot = SimpleNamespace()
    event_id = str(uuid4())
    with (
        patch.object(ui, "resolve_events_snapshot", return_value=snapshot),
        patch.object(ui, "_business_name", return_value="Организация"),
        patch.object(
            ui,
            "event_hub_actions",
            return_value=(EventHubAction("edit", "🕒 Изменить расписание", key=event_id),),
        ),
        patch.object(ui, "event_hub_text", return_value="🎥 Вебинары"),
    ):
        rendered = ui._events_message(actor)

    assert f"cpm:event-edit:{event_id}" in _commands(rendered)


def test_schedule_owner_input_resolves_to_native_edit_action() -> None:
    actor = _actor()
    event_id = str(uuid4())
    session = OwnerInputSession(
        user_id=actor.user_id,
        platform="vk",
        business_id=actor.business_id,
        action="event_schedule",
        context={"event_id": event_id},
        updated_at="2026-09-22T00:00:00+00:00",
    )

    resolved = resolve_owner_input(
        session,
        "01.10.2026 19:00-20:00\n02.10.2026 19:00-20:00",
    )

    assert resolved.action == "event-edit-text"
    assert resolved.args == (
        event_id,
        "01.10.2026 19:00-20:00\n02.10.2026 19:00-20:00",
    )


def test_native_schedule_edit_preserves_rooms_and_reschedules_notifications() -> None:
    actor = _actor()
    event_id = str(uuid4())
    existing = (
        _session(1, join_url="https://meet.example/day-1", provider_key="other", provider_label="Meet"),
        _session(2, join_url="https://meet.example/day-2", provider_key="other", provider_label="Meet"),
    )
    updated = (
        _session(1, join_url="https://meet.example/day-1", provider_key="other", provider_label="Meet"),
        _session(2, join_url="https://meet.example/day-2", provider_key="other", provider_label="Meet"),
    )
    window = SimpleNamespace(timezone_name="Europe/Amsterdam")

    with (
        patch.object(ui, "list_event_sessions", return_value=existing),
        patch.object(ui, "get_event_warmup_window", return_value=window),
        patch.object(ui, "configure_event_sessions", return_value=updated) as configure,
        patch.object(ui, "clear_owner_input") as clear,
    ):
        result = ui._event_edit_result(
            actor,
            event_id,
            "01.10.2026 19:00-20:00\n02.10.2026 19:00-20:00",
            current_platform=ConnectionPlatform.VK,
            input_surface="official",
        )

    kwargs = configure.call_args.kwargs
    assert kwargs["actor"] == actor
    assert kwargs["event_id"] == event_id
    assert kwargs["reschedule_notifications"] is True
    specs = kwargs["sessions"]
    assert len(specs) == 2
    assert specs[0].join_url == "https://meet.example/day-1"
    assert specs[1].join_url == "https://meet.example/day-2"
    assert specs[0].provider_key == "other"
    assert specs[1].provider_label == "Meet"
    clear.assert_called_once_with(
        user_id=actor.user_id,
        platform="vk",
        surface="official",
    )
    assert "Расписание вебинара обновлено" in result.text


def test_invalid_native_schedule_keeps_input_session_for_retry() -> None:
    actor = _actor()
    event_id = str(uuid4())
    existing = (
        _session(1, join_url="https://meet.example/day-1", provider_key="other", provider_label="Meet"),
        _session(2, join_url="https://meet.example/day-2", provider_key="other", provider_label="Meet"),
    )
    window = SimpleNamespace(timezone_name="Europe/Amsterdam")

    with (
        patch.object(ui, "list_event_sessions", return_value=existing),
        patch.object(ui, "get_event_warmup_window", return_value=window),
        patch.object(ui, "configure_event_sessions") as configure,
        patch.object(ui, "clear_owner_input") as clear,
    ):
        result = ui._event_edit_result(
            actor,
            event_id,
            "01.10.2026 19:00-20:00",
            current_platform=ConnectionPlatform.MAX,
            input_surface="official",
        )

    configure.assert_not_called()
    clear.assert_not_called()
    assert "Ничего не изменено" in result.text


def test_vk_and_max_schedule_prompt_use_same_canonical_shape() -> None:
    actor = _actor()
    event_id = str(uuid4())
    existing = (
        _session(1, join_url="https://meet.example/day-1", provider_key="other", provider_label="Meet"),
    )
    window = SimpleNamespace(timezone_name="Europe/Amsterdam")
    captured = []

    def begin(*args, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(text=kwargs["text"], rows=kwargs["rows"])

    with (
        patch.object(ui, "list_event_sessions", return_value=existing),
        patch.object(ui, "get_event_warmup_window", return_value=window),
        patch.object(ui, "_begin_owner_input_message", side_effect=begin),
    ):
        vk = ui._event_edit_message(
            actor,
            event_id,
            current_platform=ConnectionPlatform.VK,
            input_surface="official",
        )
        max_ui = ui._event_edit_message(
            actor,
            event_id,
            current_platform=ConnectionPlatform.MAX,
            input_surface="official",
        )

    assert vk.text == max_ui.text
    assert captured[0]["action"] == "event_schedule"
    assert captured[1]["action"] == "event_schedule"
    assert captured[0]["context"] == captured[1]["context"] == {"event_id": event_id}

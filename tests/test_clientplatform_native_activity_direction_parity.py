from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application import native_event_wizard
from clientplatform.application import native_member_interactions as ui
from clientplatform.application.owner_input import resolve_owner_input
from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.activity_directions import ActivityDirectionStatus
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.owner_input import OwnerInputSession
from clientplatform.domain.tenancy import PlatformRole, TenantContext


def _actor() -> TenantContext:
    return TenantContext(
        business_id=str(uuid4()),
        membership_id=str(uuid4()),
        user_id=101,
        role=PlatformRole.OWNER,
    )


def _direction():
    return SimpleNamespace(
        id=str(uuid4()),
        business_id=str(uuid4()),
        title="Корпоративные клиенты",
        description="Услуги и материалы для организаций.",
        status=ActivityDirectionStatus.ACTIVE,
    )


def _commands(message) -> list[str]:
    return [button.command for row in message.rows for button in row]


def test_vk_and_max_render_the_same_direction_workspace() -> None:
    actor = _actor()
    direction = _direction()
    with (
        patch.object(ui, "resolve_tenant_context", return_value=actor),
        patch.object(ui, "list_activity_directions", return_value=[direction]),
    ):
        vk = ui.render_native_member_interaction(
            actor=actor,
            raw_text="cpm:directions:0",
            interaction_key="vk-directions",
            current_platform=ConnectionPlatform.VK,
        )
        max_ui = ui.render_native_member_interaction(
            actor=actor,
            raw_text="cpm:directions:0",
            interaction_key="max-directions",
            current_platform=ConnectionPlatform.MAX,
        )

    assert vk == max_ui
    assert f"cpm:direction:{direction.id}" in _commands(vk)
    assert "cpm:direction-new" in _commands(vk)
    assert "cpm:activity-edit-help" in _commands(vk)


def test_vk_and_max_require_confirmation_before_direction_removal() -> None:
    actor = _actor()
    direction = _direction()

    results = []
    with (
        patch.object(ui, "resolve_tenant_context", return_value=actor),
        patch.object(ui, "get_activity_direction", return_value=direction),
    ):
        for platform in (ConnectionPlatform.VK, ConnectionPlatform.MAX):
            results.append(
                ui.render_native_member_interaction(
                    actor=actor,
                    raw_text=f"cpm:direction-archive:{direction.id}",
                    interaction_key=f"{platform.value}-direction-remove",
                    current_platform=platform,
                )
            )

    assert results[0] == results[1]
    assert "Удалить направление" in results[0].text
    assert f"cpm:direction-archive-ok:{direction.id}" in _commands(results[0])


def test_cross_channel_direction_read_refreshes_from_one_canonical_state() -> None:
    actor = _actor()
    before = _direction()
    after = SimpleNamespace(**{**vars(before), "title": "B2B после изменения"})

    with (
        patch.object(ui, "resolve_tenant_context", return_value=actor),
        patch.object(
            ui,
            "list_activity_directions",
            side_effect=[[before], [after]],
        ) as read_directions,
    ):
        telegram_equivalent_read = ui.render_native_member_interaction(
            actor=actor,
            raw_text="cpm:directions:0",
            interaction_key="first-channel",
            current_platform=ConnectionPlatform.VK,
        )
        other_channel_read = ui.render_native_member_interaction(
            actor=actor,
            raw_text="cpm:directions:0",
            interaction_key="second-channel",
            current_platform=ConnectionPlatform.MAX,
        )

    assert before.title in telegram_equivalent_read.text
    assert after.title in other_channel_read.text
    assert before.title not in other_channel_read.text
    assert read_directions.call_count == 2


def test_native_direction_crud_delegates_to_canonical_application_operations() -> None:
    actor = _actor()
    direction = _direction()
    updated = SimpleNamespace(**{**vars(direction), "title": "B2B"})

    with patch.object(ui, "create_activity_direction", return_value=direction) as create:
        created = ui._direction_create_result(
            actor,
            "Корпоративные клиенты",
            "Услуги и материалы для организаций.",
        )
    create.assert_called_once_with(
        actor=actor,
        title="Корпоративные клиенты",
        description="Услуги и материалы для организаций.",
    )
    assert "создано" in created.text

    with patch.object(ui, "update_activity_direction", return_value=updated) as update:
        edited = ui._direction_edit_result(
            actor,
            direction.id,
            "B2B",
            "Работа с организациями.",
        )
    update.assert_called_once_with(
        actor=actor,
        direction_id=direction.id,
        title="B2B",
        description="Работа с организациями.",
    )
    assert "обновлено" in edited.text

    archived = SimpleNamespace(**{**vars(updated), "status": ActivityDirectionStatus.ARCHIVED})
    with (
        patch.object(ui, "get_activity_direction", return_value=updated),
        patch.object(ui, "archive_activity_direction") as archive,
    ):
        confirmation = ui._direction_archive_confirm(actor, direction.id)
    archive.assert_not_called()
    assert "Удалить направление" in confirmation.text
    assert f"cpm:direction-archive-ok:{direction.id}" in _commands(confirmation)

    with patch.object(ui, "archive_activity_direction", return_value=archived) as archive:
        result = ui._direction_archive_result(actor, direction.id)
    archive.assert_called_once_with(actor=actor, direction_id=direction.id)
    assert "сохранены в архиве" in result.text

    with patch.object(ui, "restore_activity_direction", return_value=updated) as restore:
        result = ui._direction_restore_result(actor, direction.id)
    restore.assert_called_once_with(actor=actor, direction_id=direction.id)
    assert "снова активно" in result.text


def test_owner_input_preserves_direction_for_program_offering_and_direction_edits() -> None:
    actor = _actor()
    direction_id = str(uuid4())
    base = dict(
        user_id=actor.user_id,
        platform="vk",
        business_id=actor.business_id,
        updated_at="2026-09-22T00:00:00+00:00",
    )

    program = OwnerInputSession(
        **base,
        action="program_title",
        context={"direction_id": direction_id},
    )
    assert resolve_owner_input(program, "Курс").args == ("Курс", direction_id)

    offering = OwnerInputSession(
        **base,
        action="offering",
        context={"connector_key": "consultations", "direction_id": direction_id},
    )
    assert resolve_owner_input(
        offering,
        "Консультация | Личная встреча",
    ).args == (
        "consultations",
        "Консультация",
        "Личная встреча",
        direction_id,
    )

    created = OwnerInputSession(
        **base,
        action="activity_direction_create",
        context={},
    )
    assert resolve_owner_input(
        created,
        "Корпоративные клиенты | Работа с организациями",
    ).args == (
        "Корпоративные клиенты",
        "Работа с организациями",
    )

    edited = OwnerInputSession(
        **base,
        action="activity_direction_edit",
        context={"direction_id": direction_id},
    )
    assert resolve_owner_input(
        edited,
        "B2B | Новое описание",
    ).args == (
        direction_id,
        "B2B",
        "Новое описание",
    )


def test_native_program_creation_binds_selected_direction() -> None:
    actor = _actor()
    direction = _direction()
    program = SimpleNamespace(id=str(uuid4()), title="Курс")

    with patch.object(ui, "create_program", return_value=program) as create:
        ui._program_create_result(
            actor,
            "Курс",
            interaction_key="vk-program",
            direction_id=direction.id,
        )

    create.assert_called_once_with(
        actor=actor,
        title="Курс",
        idempotency_key="vk-program:program-create",
        direction_id=direction.id,
    )


def test_native_offering_creation_binds_selected_direction() -> None:
    actor = _actor()
    direction = _direction()
    capability = SimpleNamespace(
        id=str(uuid4()),
        connector_key="consultations",
        title="Консультации",
        status=CapabilityStatus.ACTIVE,
    )
    offering = SimpleNamespace(id=str(uuid4()), title="Диагностика")

    with (
        patch.object(ui, "list_business_capabilities", return_value=[capability]),
        patch.object(ui, "create_business_offering", return_value=offering) as create,
    ):
        ui._offering_new_result(
            actor,
            "consultations",
            "Диагностика",
            "Разбор задачи",
            interaction_key="max-offering",
            direction_id=direction.id,
        )

    create.assert_called_once_with(
        actor=actor,
        capability_id=capability.id,
        title="Диагностика",
        description="Разбор задачи",
        idempotency_key="max-offering:offering-create",
        direction_id=direction.id,
    )


def test_native_webinar_wizard_binds_selected_direction_to_canonical_request() -> None:
    actor = _actor()
    direction_id = str(uuid4())
    captured = {}

    def create_draft(*, actor, request):
        captured["actor"] = actor
        captured["request"] = request
        return SimpleNamespace(event_id=str(uuid4()))

    published = SimpleNamespace(
        event_id=str(uuid4()),
        registration_url=lambda _base: "https://client.example/register",
    )
    window = SimpleNamespace(max_warmup_days=7, days_until_event=10)
    context = {
        "title": "Вебинар",
        "timezone": "Europe/Amsterdam",
        "venue": "other",
        "count": "1",
        "position": "1",
        "pending_starts": "2026-10-01T19:00:00+02:00",
        "pending_ends": "2026-10-01T20:00:00+02:00",
        "topics_json": "",
        "direction_id": direction_id,
    }

    with (
        patch.object(
            native_event_wizard,
            "create_multisession_online_event_draft",
            side_effect=create_draft,
        ),
        patch.object(
            native_event_wizard,
            "publish_multisession_online_event_draft",
            return_value=published,
        ),
        patch.object(
            native_event_wizard,
            "get_event_warmup_window",
            return_value=window,
        ),
        patch.object(native_event_wizard, "_save"),
    ):
        native_event_wizard._accept_room(
            actor,
            context=context,
            raw_url="https://meet.example/room",
            platform=ConnectionPlatform.VK,
            surface="official",
        )

    assert captured["actor"] == actor
    assert captured["request"].direction_id == direction_id


def test_native_direction_selectors_offer_no_direction_and_paginate() -> None:
    actor = _actor()
    directions = [
        SimpleNamespace(
            id=str(uuid4()),
            title=f"Направление {index}",
            description="Описание",
            status=ActivityDirectionStatus.ACTIVE,
        )
        for index in range(7)
    ]
    with patch.object(ui, "list_activity_directions", return_value=directions):
        program = ui._program_direction_message(actor, 0)
        event = ui._event_direction_message(actor, 0)

    assert "cpm:program-create-dir:none" in _commands(program)
    assert "cpm:program-create-dirs:1" in _commands(program)
    assert "cpm:event-new-dir:none" in _commands(event)
    assert "cpm:event-new-dirs:1" in _commands(event)
    assert len(program.rows) <= 10
    assert len(event.rows) <= 10

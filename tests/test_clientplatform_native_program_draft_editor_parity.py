from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application import native_member_interactions as ui
from clientplatform.application.owner_input import resolve_owner_input
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.owner_input import OwnerInputSession
from clientplatform.domain.programs import ContentKind
from clientplatform.domain.tenancy import PlatformRole, TenantContext


def _actor() -> TenantContext:
    return TenantContext(
        business_id=str(uuid4()),
        membership_id=str(uuid4()),
        user_id=101,
        role=PlatformRole.OWNER,
    )


def _lesson(
    *,
    position: int = 1,
    title: str = "Введение",
    kind: ContentKind = ContentKind.TEXT,
    content_ref: str = "Первый материал",
):
    return SimpleNamespace(
        id=str(uuid4()),
        position=position,
        title=title,
        content_kind=kind,
        content_ref=content_ref,
    )


def _record(*, title: str = "Курс", lesson_count: int = 2):
    lessons = tuple(
        _lesson(position=index + 1, title=f"Урок {index + 1}")
        for index in range(lesson_count)
    )
    return SimpleNamespace(
        program=SimpleNamespace(
            id=str(uuid4()),
            business_id=str(uuid4()),
            title=title,
            status=SimpleNamespace(value="draft"),
        ),
        lessons=lessons,
    )


def _commands(message) -> list[str]:
    return [button.command for row in message.rows for button in row]


def test_program_list_opens_draft_workspace_instead_of_exposing_partial_editor() -> None:
    actor = _actor()
    record = _record(lesson_count=0)
    program = record.program
    with patch.object(ui, "list_programs", return_value=[program]):
        message = ui._programs_message(actor, 0)

    commands = _commands(message)
    assert f"cpm:program-draft:{program.id}" in commands
    assert f"cpm:program-lesson:{program.id}" not in commands
    assert f"cpm:program-publish:{program.id}" not in commands


def test_vk_and_max_render_same_fresh_draft_workspace() -> None:
    actor = _actor()
    before = _record(title="Курс до изменения")
    after = SimpleNamespace(
        program=SimpleNamespace(**{**vars(before.program), "title": "Курс после изменения"}),
        lessons=before.lessons,
    )
    with (
        patch.object(ui, "resolve_tenant_context", return_value=actor),
        patch.object(ui, "get_program_draft", side_effect=[before, after]) as get_draft,
    ):
        vk = ui.render_native_member_interaction(
            actor=actor,
            raw_text=f"cpm:program-draft:{before.program.id}",
            interaction_key="vk-draft",
            current_platform=ConnectionPlatform.VK,
        )
        max_ui = ui.render_native_member_interaction(
            actor=actor,
            raw_text=f"cpm:program-draft:{before.program.id}",
            interaction_key="max-draft",
            current_platform=ConnectionPlatform.MAX,
        )

    assert "Курс до изменения" in vk.text
    assert "Курс после изменения" in max_ui.text
    assert get_draft.call_count == 2
    assert _commands(vk) == _commands(max_ui)


def test_draft_workspace_preserves_all_telegram_editor_intents() -> None:
    actor = _actor()
    record = _record()
    with patch.object(ui, "get_program_draft", return_value=record):
        draft = ui._program_draft_message(actor, record.program.id)
        lessons = ui._program_lessons_message(actor, record.program.id, 0)

    draft_commands = _commands(draft)
    assert f"cpm:program-lessons:{record.program.id}:0" in draft_commands
    assert f"cpm:program-lesson:{record.program.id}" in draft_commands
    assert f"cpm:program-publish:{record.program.id}" in draft_commands
    assert f"cpm:program-draft-archive:{record.program.id}" in draft_commands

    lesson_commands = _commands(lessons)
    for lesson in record.lessons:
        assert f"cpm:program-lesson-detail:{lesson.id}" in lesson_commands


def test_lesson_detail_exposes_rename_replace_move_delete_and_navigation() -> None:
    actor = _actor()
    record = _record(lesson_count=3)
    lesson = record.lessons[1]
    with patch.object(ui, "get_program_draft_lesson", return_value=(record, lesson)):
        message = ui._program_lesson_detail_message(actor, lesson.id)

    commands = _commands(message)
    assert f"cpm:program-lesson-title-edit:{lesson.id}" in commands
    assert f"cpm:program-lesson-content-edit:{lesson.id}" in commands
    assert f"cpm:program-lesson-move:{lesson.id}:up" in commands
    assert f"cpm:program-lesson-move:{lesson.id}:down" in commands
    assert f"cpm:program-lesson-archive:{lesson.id}" in commands
    assert len(message.rows) <= 7


def test_native_editor_mutations_use_canonical_program_draft_boundaries() -> None:
    actor = _actor()
    record = _record(lesson_count=2)
    lesson = record.lessons[0]

    renamed = SimpleNamespace(**{**vars(lesson), "title": "Новое название"})
    with patch.object(
        ui,
        "update_program_draft_lesson_title",
        return_value=(record, renamed),
    ) as update_title:
        ui._program_lesson_title_result(actor, lesson.id, "Новое название")
    update_title.assert_called_once_with(
        actor=actor,
        lesson_id=lesson.id,
        title="Новое название",
    )

    replaced = SimpleNamespace(
        **{
            **vars(lesson),
            "content_kind": ContentKind.LINK,
            "content_ref": "https://example.test/material",
        }
    )
    with patch.object(
        ui,
        "replace_program_draft_lesson_content",
        return_value=(record, replaced),
    ) as replace_content:
        ui._program_lesson_content_result(
            actor,
            lesson.id,
            "link",
            "https://example.test/material",
        )
    replace_content.assert_called_once_with(
        actor=actor,
        lesson_id=lesson.id,
        content_kind="link",
        content_ref="https://example.test/material",
    )

    moved_record = SimpleNamespace(
        program=record.program,
        lessons=(
            SimpleNamespace(**{**vars(record.lessons[1]), "position": 1}),
            SimpleNamespace(**{**vars(lesson), "position": 2}),
        ),
    )
    with patch.object(ui, "move_program_draft_lesson", return_value=moved_record) as move:
        ui._program_lesson_move_result(actor, lesson.id, "down")
    move.assert_called_once_with(actor=actor, lesson_id=lesson.id, direction="down")

    with (
        patch.object(ui, "get_program_draft_lesson", return_value=(record, lesson)),
        patch.object(ui, "archive_program_draft_lesson", return_value=record) as archive_lesson,
    ):
        ui._program_lesson_archive_result(actor, lesson.id)
    archive_lesson.assert_called_once_with(actor=actor, lesson_id=lesson.id)

    with patch.object(ui, "archive_program_draft", return_value=record.program) as archive_draft:
        ui._program_draft_archive_result(actor, record.program.id)
    archive_draft.assert_called_once_with(actor=actor, program_id=record.program.id)


def test_owner_input_resolves_native_lesson_title_and_content_edits() -> None:
    actor = _actor()
    lesson_id = str(uuid4())
    base = dict(
        user_id=actor.user_id,
        platform="vk",
        business_id=actor.business_id,
        updated_at="2026-09-22T00:00:00+00:00",
    )

    title = OwnerInputSession(
        **base,
        action="program_lesson_title_edit",
        context={"lesson_id": lesson_id},
    )
    assert resolve_owner_input(title, "Новое название").action == "program-lesson-title-text"
    assert resolve_owner_input(title, "Новое название").args == (
        lesson_id,
        "Новое название",
    )

    content = OwnerInputSession(
        **base,
        action="program_lesson_content_edit",
        context={"lesson_id": lesson_id, "content_kind": "text"},
    )
    resolved = resolve_owner_input(content, "Новый материал")
    assert resolved.action == "program-lesson-content-text"
    assert resolved.args == (lesson_id, "text", "Новый материал")


def test_native_editor_routes_are_parser_admitted() -> None:
    actions = {
        "program-draft",
        "program-draft-archive",
        "program-draft-archive-ok",
        "program-lessons",
        "program-lesson-detail",
        "program-lesson-title-edit",
        "program-lesson-title-text",
        "program-lesson-content-edit",
        "program-lesson-content-kind",
        "program-lesson-content-text",
        "program-lesson-move",
        "program-lesson-archive",
        "program-lesson-archive-ok",
    }
    for action in actions:
        assert ui.parse_native_member_interaction(f"cpm:{action}").action == action

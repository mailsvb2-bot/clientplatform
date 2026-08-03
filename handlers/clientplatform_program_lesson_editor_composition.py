from __future__ import annotations

import importlib

builder = importlib.import_module(".clientplatform_program_builder", __package__)
editor = importlib.import_module(".clientplatform_program_lesson_editor", __package__)
control = importlib.import_module(".clientplatform_control", __package__)


def _draft_keyboard_with_lesson_editor(record):
    business_id = record.program.business_id
    program_id = record.program.id
    rows: list[list[tuple[str, str]]] = []
    if len(record.lessons) < builder._MAX_LESSONS_PER_PROGRAM:
        rows.append(
            [
                (
                    "Добавить ещё урок",
                    builder._program_callback("dadd", business_id, program_id),
                )
            ]
        )
    if record.lessons:
        rows.append(
            [
                (
                    "✏️ Заменить или удалить материал",
                    editor._program_callback(
                        "dless",
                        business_id,
                        program_id,
                        0,
                    ),
                )
            ]
        )
    rows.extend(
        [
            [
                (
                    "Опубликовать программу",
                    builder._program_callback("dpub", business_id, program_id),
                )
            ],
            [
                (
                    "Удалить черновик",
                    builder._program_callback("darc", business_id, program_id),
                )
            ],
        ]
    )
    return control._keyboard(rows)


if not bool(getattr(builder, "_draft_lesson_editor_composed", False)):
    builder._draft_keyboard = _draft_keyboard_with_lesson_editor
    builder._draft_lesson_editor_composed = True

router = editor.router

__all__ = ["router"]

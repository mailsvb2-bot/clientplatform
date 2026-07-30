from __future__ import annotations

import base64
import importlib
from typing import Any
from uuid import UUID

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from clientplatform.application.programs import (
    append_lesson,
    create_program,
    get_program_for_owner,
    list_owner_programs,
    publish_program,
)
from clientplatform.domain.programs import Lesson, ProgramStatus, ProgramSummary

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_program_builder")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())


class ClientPlatformProgramBuilderState(StatesGroup):
    review = State()


def _compact_uuid(value: str) -> str:
    return base64.urlsafe_b64encode(UUID(str(value)).bytes).decode("ascii").rstrip("=")


def _expanded_uuid(value: str) -> str:
    if len(value) != 22:
        raise ValueError("Некорректная ссылка на программу.")
    try:
        decoded = base64.urlsafe_b64decode(value + "==")
        return str(UUID(bytes=decoded))
    except (ValueError, TypeError):
        raise ValueError("Некорректная ссылка на программу.") from None


def _draft_callback(action: str, *, business_id: str, program_id: str) -> str:
    if action not in {"open", "add", "publish", "exit"}:
        raise ValueError("Unsupported program builder action")
    callback = (
        f"cp:pb:{action}:{_compact_uuid(business_id)}:{_compact_uuid(program_id)}"
    )
    if len(callback.encode("utf-8")) > 64:
        raise ValueError("Program builder callback exceeds Telegram limit")
    return callback


def _parse_draft_callback(data: str | None, *, action: str) -> tuple[str, str]:
    parts = str(data or "").split(":")
    if len(parts) != 5 or parts[:3] != ["cp", "pb", action]:
        raise ValueError("Некорректная команда конструктора программы.")
    return _expanded_uuid(parts[3]), _expanded_uuid(parts[4])


def _draft_keyboard(*, business_id: str, program_id: str):
    return control._keyboard(
        [
            [
                (
                    "Добавить ещё урок",
                    _draft_callback(
                        "add",
                        business_id=business_id,
                        program_id=program_id,
                    ),
                )
            ],
            [
                (
                    "Опубликовать программу",
                    _draft_callback(
                        "publish",
                        business_id=business_id,
                        program_id=program_id,
                    ),
                )
            ],
            [
                (
                    "Сохранить черновик и выйти",
                    _draft_callback(
                        "exit",
                        business_id=business_id,
                        program_id=program_id,
                    ),
                )
            ],
        ]
    )


def _programs_keyboard(*, business_id: str, programs: list[ProgramSummary]):
    rows: list[list[tuple[str, str]]] = []
    for program in programs:
        if program.status is not ProgramStatus.DRAFT:
            continue
        title = program.title if len(program.title) <= 34 else f"{program.title[:31]}…"
        rows.append(
            [
                (
                    f"Продолжить: {title}",
                    _draft_callback(
                        "open",
                        business_id=business_id,
                        program_id=program.id,
                    ),
                )
            ]
        )
    rows.extend(
        [
            [("Создать программу", f"cp:progadd:{business_id}")],
            [("В кабинет", f"cp:dashboard:{business_id}")],
        ]
    )
    return control._keyboard(rows)


def _lesson_kind_label(lesson: Lesson) -> str:
    labels = {
        "text": "текст",
        "audio": "аудио",
        "video": "видео",
        "image": "изображение",
        "document": "документ",
    }
    return labels.get(lesson.content_kind.value, lesson.content_kind.value)


def _draft_text(program: ProgramSummary, lessons: list[Lesson]) -> str:
    lines = [
        f"Черновик программы «{program.title}».",
        f"Уроков: {len(lessons)}.",
    ]
    if lessons:
        lines.extend(
            [
                "",
                *[
                    f"{lesson.sequence}. {lesson.title} — {_lesson_kind_label(lesson)}"
                    for lesson in lessons
                ],
            ]
        )
    lines.extend(
        [
            "",
            "Добавьте следующий урок или опубликуйте программу, когда она готова.",
        ]
    )
    return "\n".join(lines)


async def _load_owned_draft(
    *,
    user_id: int,
    business_id: str,
    program_id: str,
) -> tuple[ProgramSummary, list[Lesson]]:
    program, lessons = await control._run_blocking(
        get_program_for_owner,
        user_id=user_id,
        business_id=business_id,
        program_id=program_id,
    )
    if program.status is not ProgramStatus.DRAFT:
        raise ValueError("Эта программа уже опубликована и больше не является черновиком.")
    return program, lessons


async def _show_draft(
    message: Message,
    *,
    state: FSMContext,
    business_id: str,
    program_id: str,
    program: ProgramSummary | None = None,
    lessons: list[Lesson] | None = None,
) -> None:
    if program is None or lessons is None:
        program, lessons = await _load_owned_draft(
            user_id=control._user_id(message),
            business_id=business_id,
            program_id=program_id,
        )
    await state.clear()
    await state.update_data(
        business_id=business_id,
        program_id=program_id,
        program_title=program.title,
    )
    await state.set_state(ClientPlatformProgramBuilderState.review)
    await message.answer(
        _draft_text(program, lessons),
        reply_markup=_draft_keyboard(
            business_id=business_id,
            program_id=program_id,
        ),
    )


@router.callback_query(F.data.startswith("cp:programs:"))
async def show_programs(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = str(callback.data or "").removeprefix("cp:programs:")
    user_id = int(callback.from_user.id)
    programs = await control._run_blocking(
        list_owner_programs,
        user_id=user_id,
        business_id=business_id,
    )
    await state.clear()
    await callback.answer()
    text = (
        "Программ пока нет. Создайте первую."
        if not programs
        else "Ваши программы:\n\n"
        + "\n".join(control._format_program(program) for program in programs)
    )
    await control._callback_message(callback).answer(
        text,
        reply_markup=_programs_keyboard(
            business_id=business_id,
            programs=programs,
        ),
    )


@router.callback_query(F.data.startswith("cp:progadd:"))
async def begin_program(callback: CallbackQuery, state: FSMContext) -> None:
    business_id = str(callback.data or "").removeprefix("cp:progadd:")
    await control._resolve_business(int(callback.from_user.id), business_id)
    await state.clear()
    await state.update_data(business_id=business_id)
    await state.set_state(control.ClientPlatformControlState.program_title)
    await callback.answer()
    await control._callback_message(callback).answer("Напишите название программы.")


@router.message(control.ClientPlatformControlState.program_title)
async def capture_program_title(message: Message, state: FSMContext) -> None:
    title = control._message_text(message, field="Название программы")
    await state.update_data(program_title=title)
    await state.set_state(control.ClientPlatformControlState.lesson_title)
    await message.answer("Напишите название первого урока.")


@router.message(control.ClientPlatformControlState.lesson_title)
async def capture_lesson_title(message: Message, state: FSMContext) -> None:
    title = control._message_text(message, field="Название урока")
    await state.update_data(lesson_title=title)
    await state.set_state(control.ClientPlatformControlState.lesson_content)
    await message.answer(
        "Отправьте содержание урока: текст, аудио, голосовое сообщение, видео, изображение или документ."
    )


@router.message(control.ClientPlatformControlState.lesson_content)
async def capture_lesson_content(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data["business_id"])
    program_id = str(data.get("program_id") or "")
    content_kind, content_value = control._message_content(message)
    user_id = control._user_id(message)

    if not program_id:
        program = await control._run_blocking(
            create_program,
            user_id=user_id,
            business_id=business_id,
            title=str(data["program_title"]),
        )
        program_id = program.id
        await state.update_data(program_id=program_id)
    else:
        await _load_owned_draft(
            user_id=user_id,
            business_id=business_id,
            program_id=program_id,
        )

    await control._run_blocking(
        append_lesson,
        user_id=user_id,
        business_id=business_id,
        program_id=program_id,
        title=str(data["lesson_title"]),
        content_kind=content_kind,
        content_value=content_value,
    )
    program, lessons = await _load_owned_draft(
        user_id=user_id,
        business_id=business_id,
        program_id=program_id,
    )
    await _show_draft(
        message,
        state=state,
        business_id=business_id,
        program_id=program_id,
        program=program,
        lessons=lessons,
    )


@router.callback_query(F.data.startswith("cp:pb:open:"))
async def open_draft(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, program_id = _parse_draft_callback(
        callback.data,
        action="open",
    )
    await callback.answer()
    await _show_draft(
        control._callback_message(callback),
        state=state,
        business_id=business_id,
        program_id=program_id,
    )


@router.callback_query(F.data.startswith("cp:pb:add:"))
async def add_lesson(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, program_id = _parse_draft_callback(
        callback.data,
        action="add",
    )
    program, _ = await _load_owned_draft(
        user_id=int(callback.from_user.id),
        business_id=business_id,
        program_id=program_id,
    )
    await state.clear()
    await state.update_data(
        business_id=business_id,
        program_id=program_id,
        program_title=program.title,
    )
    await state.set_state(control.ClientPlatformControlState.lesson_title)
    await callback.answer()
    await control._callback_message(callback).answer("Напишите название следующего урока.")


@router.callback_query(F.data.startswith("cp:pb:publish:"))
async def publish_draft(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, program_id = _parse_draft_callback(
        callback.data,
        action="publish",
    )
    await _load_owned_draft(
        user_id=int(callback.from_user.id),
        business_id=business_id,
        program_id=program_id,
    )
    program = await control._run_blocking(
        publish_program,
        user_id=int(callback.from_user.id),
        business_id=business_id,
        program_id=program_id,
    )
    await state.clear()
    await callback.answer("Программа опубликована")
    await control._callback_message(callback).answer(
        f"Программа «{program.title}» опубликована.",
        reply_markup=control._keyboard(
            [
                [("Все программы", f"cp:programs:{business_id}")],
                [("В кабинет", f"cp:dashboard:{business_id}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cp:pb:exit:"))
async def exit_draft(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, program_id = _parse_draft_callback(
        callback.data,
        action="exit",
    )
    program, lessons = await _load_owned_draft(
        user_id=int(callback.from_user.id),
        business_id=business_id,
        program_id=program_id,
    )
    await state.clear()
    await callback.answer()
    await control._callback_message(callback).answer(
        f"Черновик «{program.title}» сохранён. Уроков: {len(lessons)}.",
        reply_markup=control._keyboard(
            [
                [("Все программы", f"cp:programs:{business_id}")],
                [("В кабинет", f"cp:dashboard:{business_id}")],
            ]
        ),
    )


__all__ = [
    "ClientPlatformProgramBuilderState",
    "add_lesson",
    "begin_program",
    "capture_lesson_content",
    "capture_lesson_title",
    "capture_program_title",
    "exit_draft",
    "open_draft",
    "publish_draft",
    "router",
    "show_programs",
]

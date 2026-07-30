from __future__ import annotations

import asyncio
import importlib
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from clientplatform.application.program_builder import (
    ProgramLessonInput,
    create_multi_lesson_program,
)
from clientplatform.domain.programs import (
    ContentKind,
    normalize_lesson_title,
    normalize_program_title,
)

control = importlib.import_module(".clientplatform_control", __package__)

router = Router(name="clientplatform_program_builder")
router.message.filter(control.ClientPlatformControlEnabled())
router.callback_query.filter(control.ClientPlatformControlEnabled())

_MAX_LESSONS_PER_PROGRAM = 100
_REVIEW_LESSON_LIMIT = 20


class ClientPlatformProgramBuilderState(StatesGroup):
    program_title = State()
    lesson_title = State()
    lesson_content = State()
    review = State()


def _review_keyboard(*, can_add: bool):
    rows: list[list[tuple[str, str]]] = []
    if can_add:
        rows.append([("Добавить ещё урок", "cp:pbuild:add")])
    rows.extend(
        [
            [("Опубликовать программу", "cp:pbuild:publish")],
            [("Отменить создание", "cp:pbuild:cancel")],
        ]
    )
    return control._keyboard(rows)


def _content_kind_label(value: str) -> str:
    return {
        ContentKind.TEXT.value: "текст",
        ContentKind.AUDIO.value: "аудио",
        ContentKind.VIDEO.value: "видео",
        ContentKind.IMAGE.value: "изображение",
        ContentKind.DOCUMENT.value: "документ",
        ContentKind.LINK.value: "ссылка",
        ContentKind.TASK.value: "задание",
        ContentKind.MIXED.value: "смешанный материал",
    }.get(value, value)


def _review_text(*, program_title: str, lessons: list[dict[str, str]]) -> str:
    visible = lessons[:_REVIEW_LESSON_LIMIT]
    lines = [
        f"Программа «{program_title}»",
        f"Уроков добавлено: {len(lessons)}.",
        "",
    ]
    lines.extend(
        f"{index}. {lesson['title'][:80]} — {_content_kind_label(lesson['content_kind'])}"
        for index, lesson in enumerate(visible, start=1)
    )
    hidden = len(lessons) - len(visible)
    if hidden > 0:
        lines.append(f"…и ещё {hidden} уроков.")
    lines.extend(
        [
            "",
            "Добавьте следующий урок или опубликуйте готовую программу.",
        ]
    )
    return "\n".join(lines)


def _session_data(data: dict[str, Any]) -> tuple[str, str, list[dict[str, str]]] | None:
    business_id = str(data.get("business_id") or "").strip()
    program_title = str(data.get("program_title") or "").strip()
    raw_lessons = data.get("lessons")
    if not business_id or not program_title or not isinstance(raw_lessons, list):
        return None

    lessons: list[dict[str, str]] = []
    for raw in raw_lessons:
        if not isinstance(raw, dict):
            return None
        title = str(raw.get("title") or "").strip()
        content_kind = str(raw.get("content_kind") or "").strip()
        content_ref = str(raw.get("content_ref") or "").strip()
        if not title or not content_kind or not content_ref:
            return None
        lessons.append(
            {
                "title": title,
                "content_kind": content_kind,
                "content_ref": content_ref,
            }
        )
    return business_id, program_title, lessons


async def _closed_builder(callback: CallbackQuery) -> None:
    await callback.answer(
        "Конструктор уже закрыт. Откройте раздел программ заново.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("cp:progadd:"))
async def begin_program(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data or "").removeprefix("cp:progadd:")
    business_id = control._token_uuid(business_token)
    await control._actor(int(callback.from_user.id), business_id)
    await state.clear()
    await state.update_data(business_id=business_id, lessons=[])
    await state.set_state(ClientPlatformProgramBuilderState.program_title)
    await callback.answer()
    await control._callback_message(callback).answer("Напишите название программы.")


@router.message(ClientPlatformProgramBuilderState.program_title)
async def capture_program_title(message: Message, state: FSMContext) -> None:
    try:
        title = normalize_program_title(str(message.text or ""))
    except ValueError:
        await message.answer("Название программы должно содержать от 1 до 200 символов.")
        return
    await state.update_data(program_title=title)
    await state.set_state(ClientPlatformProgramBuilderState.lesson_title)
    await message.answer("Как называется первый урок?")


@router.message(ClientPlatformProgramBuilderState.lesson_title)
async def capture_lesson_title(message: Message, state: FSMContext) -> None:
    try:
        title = normalize_lesson_title(str(message.text or ""))
    except ValueError:
        await message.answer("Название урока должно содержать от 1 до 200 символов.")
        return
    await state.update_data(lesson_title=title)
    await state.set_state(ClientPlatformProgramBuilderState.lesson_content)
    await message.answer(
        "Отправьте материал урока: текст, аудио, голосовое сообщение, видео, "
        "изображение или документ."
    )


@router.message(ClientPlatformProgramBuilderState.lesson_content)
async def capture_lesson_content(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    session = _session_data(data)
    lesson_title = str(data.get("lesson_title") or "").strip()
    if session is None or not lesson_title:
        await state.clear()
        await message.answer("Конструктор был закрыт. Откройте раздел программ заново.")
        return

    business_id, program_title, lessons = session
    if len(lessons) >= _MAX_LESSONS_PER_PROGRAM:
        await state.set_state(ClientPlatformProgramBuilderState.review)
        await message.answer(
            "В одной программе может быть не более 100 уроков.",
            reply_markup=_review_keyboard(can_add=False),
        )
        return

    try:
        content_kind, content_ref = control._message_content(message)
    except ValueError:
        await message.answer(
            "Поддерживаются текст, аудио, голосовое сообщение, видео, "
            "изображение или документ."
        )
        return

    lessons.append(
        {
            "title": lesson_title,
            "content_kind": content_kind.value,
            "content_ref": content_ref,
        }
    )
    await state.update_data(lessons=lessons, lesson_title="")
    await state.set_state(ClientPlatformProgramBuilderState.review)
    await message.answer(
        _review_text(program_title=program_title, lessons=lessons),
        reply_markup=_review_keyboard(
            can_add=len(lessons) < _MAX_LESSONS_PER_PROGRAM,
        ),
    )


@router.callback_query(F.data == "cp:pbuild:add")
async def add_lesson(callback: CallbackQuery, state: FSMContext) -> None:
    session = _session_data(await state.get_data())
    if session is None:
        await _closed_builder(callback)
        return
    business_id, _program_title, lessons = session
    if len(lessons) >= _MAX_LESSONS_PER_PROGRAM:
        await callback.answer("Достигнут предел: 100 уроков.", show_alert=True)
        return
    await control._actor(int(callback.from_user.id), business_id)
    await state.update_data(lesson_title="")
    await state.set_state(ClientPlatformProgramBuilderState.lesson_title)
    await callback.answer()
    await control._callback_message(callback).answer("Как называется следующий урок?")


@router.callback_query(F.data == "cp:pbuild:publish")
async def publish_program(callback: CallbackQuery, state: FSMContext) -> None:
    session = _session_data(await state.get_data())
    if session is None:
        await _closed_builder(callback)
        return
    business_id, program_title, lessons = session
    if not lessons:
        await callback.answer("Добавьте хотя бы один урок.", show_alert=True)
        return

    actor = await control._actor(int(callback.from_user.id), business_id)
    record = await asyncio.to_thread(
        create_multi_lesson_program,
        actor=actor,
        program_title=program_title,
        lessons=tuple(
            ProgramLessonInput(
                title=lesson["title"],
                content_kind=lesson["content_kind"],
                content_ref=lesson["content_ref"],
            )
            for lesson in lessons
        ),
    )
    await state.clear()
    await callback.answer("Программа опубликована")
    message = control._callback_message(callback)
    await message.answer(
        f"Программа «{record.program.title}» опубликована. "
        f"Уроков: {len(record.lessons)}."
    )
    await control._send_dashboard(
        message,
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data == "cp:pbuild:cancel")
async def cancel_program(callback: CallbackQuery, state: FSMContext) -> None:
    session = _session_data(await state.get_data())
    if session is None:
        await _closed_builder(callback)
        return
    business_id, _program_title, _lessons = session
    await control._actor(int(callback.from_user.id), business_id)
    await state.clear()
    await callback.answer("Создание отменено")
    message = control._callback_message(callback)
    await message.answer("Черновик удалён. В базе ничего не создавалось.")
    await control._send_dashboard(
        message,
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


__all__ = [
    "ClientPlatformProgramBuilderState",
    "add_lesson",
    "begin_program",
    "cancel_program",
    "capture_lesson_content",
    "capture_lesson_title",
    "capture_program_title",
    "publish_program",
    "router",
]

from __future__ import annotations

import asyncio
import importlib
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from clientplatform.application.programs import (
    add_program_lesson,
    archive_program_draft,
    create_program,
    get_program_draft,
    list_program_drafts,
    list_programs,
    publish_program,
)
from clientplatform.domain.programs import (
    ContentKind,
    Program,
    ProgramRecord,
    ProgramStatus,
    normalize_content_ref,
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


def _program_callback(action: str, business_id: str, program_id: str) -> str:
    return (
        f"cp:{action}:{control._uuid_token(business_id)}:"
        f"{control._uuid_token(program_id)}"
    )


def _draft_keyboard(record: ProgramRecord):
    business_id = record.program.business_id
    program_id = record.program.id
    rows: list[list[tuple[str, str]]] = []
    if len(record.lessons) < _MAX_LESSONS_PER_PROGRAM:
        rows.append(
            [
                (
                    "Добавить ещё урок",
                    _program_callback("dadd", business_id, program_id),
                )
            ]
        )
    rows.extend(
        [
            [
                (
                    "Опубликовать программу",
                    _program_callback("dpub", business_id, program_id),
                )
            ],
            [
                (
                    "Удалить черновик",
                    _program_callback("darc", business_id, program_id),
                )
            ],
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


def _review_text(record: ProgramRecord) -> str:
    visible = record.lessons[:_REVIEW_LESSON_LIMIT]
    lines = [
        f"Черновик «{record.program.title}»",
        f"Уроков сохранено: {len(record.lessons)}.",
        "",
    ]
    lines.extend(
        f"{lesson.position}. {lesson.title[:80]} — "
        f"{_content_kind_label(lesson.content_kind.value)}"
        for lesson in visible
    )
    hidden = len(record.lessons) - len(visible)
    if hidden > 0:
        lines.append(f"…и ещё {hidden} уроков.")
    lines.extend(
        [
            "",
            "Черновик сохранён. Можно продолжить позже через раздел «Программы».",
        ]
    )
    return "\n".join(lines)


def _session_ids(data: dict[str, Any]) -> tuple[str, str] | None:
    business_id = str(data.get("business_id") or "").strip()
    program_id = str(data.get("program_id") or "").strip()
    if not business_id or not program_id:
        return None
    return business_id, program_id


def _program_lines(programs: list[Program]) -> str:
    markers = {
        ProgramStatus.DRAFT: "📝",
        ProgramStatus.ACTIVE: "✅",
    }
    return "\n".join(
        f"{markers.get(program.status, '•')} {program.title}"
        for program in programs
    ) or "Пока нет программ."


def _programs_keyboard(
    *,
    business_id: str,
    drafts: list[Program],
    active: list[Program],
):
    business_token = control._uuid_token(business_id)
    rows: list[list[tuple[str, str]]] = [
        [("Создать программу", f"cp:progadd:{business_token}")]
    ]
    if drafts:
        rows.append(
            [
                (
                    f"Черновики · {len(drafts)}",
                    f"cp:drafts:{business_token}",
                )
            ]
        )
    if active:
        rows.append([("Выдать клиенту", f"cp:deliver:{business_token}")])
    return control._keyboard(rows)


async def _load_draft(
    *,
    user_id: int,
    business_id: str,
    program_id: str,
) -> ProgramRecord:
    actor = await control._actor(user_id, business_id)
    return await asyncio.to_thread(
        get_program_draft,
        actor=actor,
        program_id=program_id,
    )


async def _send_draft_review(message: Message, record: ProgramRecord) -> None:
    await message.answer(
        _review_text(record),
        reply_markup=_draft_keyboard(record),
    )


def _callback_program_ids(callback: CallbackQuery) -> tuple[str, str]:
    parts = str(callback.data or "").split(":", 3)
    return control._token_uuid(parts[2]), control._token_uuid(parts[3])


@router.callback_query(F.data.startswith("cp:cap:"), F.data.endswith(":programs"))
async def open_programs(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, business_token, _connector_key = str(callback.data).split(":", 3)
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    programs = await asyncio.to_thread(list_programs, actor=actor)
    drafts = [item for item in programs if item.status == ProgramStatus.DRAFT]
    active = [item for item in programs if item.status == ProgramStatus.ACTIVE]
    await state.clear()
    await callback.answer()
    await control._callback_message(callback).answer(
        f"Программы\n\n{_program_lines(programs)}",
        reply_markup=_programs_keyboard(
            business_id=business_id,
            drafts=drafts,
            active=active,
        ),
    )


@router.callback_query(F.data.startswith("cp:deliver:"))
async def choose_active_program_for_delivery(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    programs = [
        item
        for item in await asyncio.to_thread(list_programs, actor=actor)
        if item.status == ProgramStatus.ACTIVE
    ]
    await state.update_data(business_id=business_id)
    await callback.answer()
    if not programs:
        await control._callback_message(callback).answer(
            "Сначала опубликуйте хотя бы одну программу."
        )
        return
    await control._callback_message(callback).answer(
        "Какую программу выдать?",
        reply_markup=control._keyboard(
            [
                [
                    (
                        program.title,
                        f"cp:sendp:{business_token}:"
                        f"{control._uuid_token(program.id)}",
                    )
                ]
                for program in programs
            ]
        ),
    )


@router.callback_query(F.data.startswith("cp:drafts:"))
async def open_drafts(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = control._token_uuid(business_token)
    actor = await control._actor(int(callback.from_user.id), business_id)
    drafts = await asyncio.to_thread(list_program_drafts, actor=actor)
    await state.clear()
    await callback.answer()
    message = control._callback_message(callback)
    if not drafts:
        await message.answer("Сохранённых черновиков нет.")
        return
    await message.answer(
        "Выберите черновик:",
        reply_markup=control._keyboard(
            [
                [
                    (
                        draft.title[:42],
                        _program_callback("dopen", business_id, draft.id),
                    )
                ]
                for draft in drafts
            ]
        ),
    )


@router.callback_query(F.data.startswith("cp:dopen:"))
async def open_draft(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, program_id = _callback_program_ids(callback)
    record = await _load_draft(
        user_id=int(callback.from_user.id),
        business_id=business_id,
        program_id=program_id,
    )
    await state.clear()
    await state.update_data(business_id=business_id, program_id=program_id)
    await state.set_state(ClientPlatformProgramBuilderState.review)
    await callback.answer()
    await _send_draft_review(control._callback_message(callback), record)


@router.callback_query(F.data.startswith("cp:progadd:"))
async def begin_program(callback: CallbackQuery, state: FSMContext) -> None:
    business_token = str(callback.data or "").removeprefix("cp:progadd:")
    business_id = control._token_uuid(business_token)
    await control._actor(int(callback.from_user.id), business_id)
    await state.clear()
    await state.update_data(business_id=business_id)
    await state.set_state(ClientPlatformProgramBuilderState.program_title)
    await callback.answer()
    await control._callback_message(callback).answer("Напишите название программы.")


@router.message(ClientPlatformProgramBuilderState.program_title)
async def capture_program_title(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    business_id = str(data.get("business_id") or "").strip()
    if not business_id:
        await state.clear()
        await message.answer("Конструктор был закрыт. Откройте раздел программ заново.")
        return
    try:
        title = normalize_program_title(str(message.text or ""))
    except ValueError:
        await message.answer("Название программы должно содержать от 1 до 200 символов.")
        return
    actor = await control._actor(int(message.from_user.id), business_id)
    program = await asyncio.to_thread(create_program, actor=actor, title=title)
    await state.update_data(program_id=program.id)
    await state.set_state(ClientPlatformProgramBuilderState.lesson_title)
    await message.answer(
        "Черновик создан и будет сохраняться автоматически. "
        "Как называется первый урок?"
    )


@router.message(ClientPlatformProgramBuilderState.lesson_title)
async def capture_lesson_title(message: Message, state: FSMContext) -> None:
    if _session_ids(await state.get_data()) is None:
        await state.clear()
        await message.answer("Конструктор был закрыт. Откройте раздел программ заново.")
        return
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
    session = _session_ids(data)
    lesson_title = str(data.get("lesson_title") or "").strip()
    if session is None or not lesson_title:
        await state.clear()
        await message.answer("Конструктор был закрыт. Откройте раздел программ заново.")
        return
    business_id, program_id = session
    record = await _load_draft(
        user_id=int(message.from_user.id),
        business_id=business_id,
        program_id=program_id,
    )
    if len(record.lessons) >= _MAX_LESSONS_PER_PROGRAM:
        await state.set_state(ClientPlatformProgramBuilderState.review)
        await message.answer(
            "В одной программе может быть не более 100 уроков.",
            reply_markup=_draft_keyboard(record),
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
    try:
        normalized_ref = normalize_content_ref(content_ref)
    except ValueError:
        await message.answer(
            "Материал урока слишком длинный. Отправьте не более 2048 символов "
            "или приложите материал отдельным файлом."
        )
        return

    actor = await control._actor(int(message.from_user.id), business_id)
    await asyncio.to_thread(
        add_program_lesson,
        actor=actor,
        program_id=program_id,
        title=lesson_title,
        content_kind=content_kind,
        content_ref=normalized_ref,
    )
    record = await asyncio.to_thread(
        get_program_draft,
        actor=actor,
        program_id=program_id,
    )
    await state.update_data(lesson_title="")
    await state.set_state(ClientPlatformProgramBuilderState.review)
    await _send_draft_review(message, record)


@router.callback_query(F.data.startswith("cp:dadd:"))
async def add_lesson(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, program_id = _callback_program_ids(callback)
    record = await _load_draft(
        user_id=int(callback.from_user.id),
        business_id=business_id,
        program_id=program_id,
    )
    if len(record.lessons) >= _MAX_LESSONS_PER_PROGRAM:
        await callback.answer("Достигнут предел: 100 уроков.", show_alert=True)
        return
    await state.clear()
    await state.update_data(business_id=business_id, program_id=program_id)
    await state.set_state(ClientPlatformProgramBuilderState.lesson_title)
    await callback.answer()
    await control._callback_message(callback).answer("Как называется следующий урок?")


@router.callback_query(F.data.startswith("cp:dpub:"))
async def publish_draft(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, program_id = _callback_program_ids(callback)
    actor = await control._actor(int(callback.from_user.id), business_id)
    record = await asyncio.to_thread(
        get_program_draft,
        actor=actor,
        program_id=program_id,
    )
    if not record.lessons:
        await callback.answer("Добавьте хотя бы один урок.", show_alert=True)
        return
    program = await asyncio.to_thread(
        publish_program,
        actor=actor,
        program_id=program_id,
    )
    await state.clear()
    await callback.answer("Программа опубликована")
    message = control._callback_message(callback)
    await message.answer(
        f"Программа «{program.title}» опубликована. Уроков: {len(record.lessons)}."
    )
    await control._send_dashboard(
        message,
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cp:darc:"))
async def archive_draft(callback: CallbackQuery, state: FSMContext) -> None:
    business_id, program_id = _callback_program_ids(callback)
    actor = await control._actor(int(callback.from_user.id), business_id)
    program = await asyncio.to_thread(
        archive_program_draft,
        actor=actor,
        program_id=program_id,
    )
    await state.clear()
    await callback.answer("Черновик удалён")
    message = control._callback_message(callback)
    await message.answer(f"Черновик «{program.title}» удалён.")
    await control._send_dashboard(
        message,
        user_id=int(callback.from_user.id),
        business_id=business_id,
    )


@router.callback_query(F.data.startswith("cp:pbuild:"))
async def obsolete_builder_callback(callback: CallbackQuery) -> None:
    await callback.answer(
        "Эта кнопка устарела. Откройте раздел программ и выберите черновик заново.",
        show_alert=True,
    )


__all__ = [
    "ClientPlatformProgramBuilderState",
    "add_lesson",
    "archive_draft",
    "begin_program",
    "capture_lesson_content",
    "capture_lesson_title",
    "capture_program_title",
    "choose_active_program_for_delivery",
    "open_draft",
    "open_drafts",
    "open_programs",
    "publish_draft",
    "router",
]

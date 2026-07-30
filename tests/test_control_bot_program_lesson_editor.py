from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

import pytest

from clientplatform.domain.programs import ContentKind, LessonStatus, ProgramStatus

editor = importlib.import_module("handlers.clientplatform_program_lesson_editor")


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self, *, text: str | None = None, user_id: int = 101) -> None:
        self.from_user = FakeUser(user_id)
        self.text = text
        self.audio = None
        self.voice = None
        self.video = None
        self.document = None
        self.photo: list[Any] = []
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class FakeCallback:
    def __init__(self, data: str, *, user_id: int = 101) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(user_id=user_id)
        self.answers: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((args, kwargs))


class FakeState:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = dict(data or {})
        self.states: list[Any] = []
        self.clear_count = 0

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def set_state(self, value: Any) -> None:
        self.states.append(value)

    async def clear(self) -> None:
        self.clear_count += 1
        self.data.clear()


async def direct_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


class FakeLessonStore:
    def __init__(self, lesson_count: int = 10) -> None:
        self.business_id = str(uuid4())
        self.program_id = str(uuid4())
        self.program = SimpleNamespace(
            id=self.program_id,
            business_id=self.business_id,
            title="Курс спокойствия",
            status=ProgramStatus.DRAFT,
        )
        self.lessons = [
            SimpleNamespace(
                id=str(uuid4()),
                business_id=self.business_id,
                program_id=self.program_id,
                position=index + 1,
                title=f"Урок {index + 1}",
                content_kind=ContentKind.TEXT,
                content_ref=f"Материал {index + 1}",
                status=LessonStatus.ACTIVE,
            )
            for index in range(lesson_count)
        ]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def record(self) -> Any:
        return SimpleNamespace(program=self.program, lessons=tuple(self.lessons))

    def _lesson(self, lesson_id: str) -> Any:
        return next(item for item in self.lessons if item.id == lesson_id)

    def get_program_draft(self, **kwargs: Any) -> Any:
        self.calls.append(("get_program", kwargs))
        assert kwargs["program_id"] == self.program_id
        return self.record()

    def get_program_draft_lesson(self, **kwargs: Any) -> tuple[Any, Any]:
        self.calls.append(("get_lesson", kwargs))
        return self.record(), self._lesson(kwargs["lesson_id"])

    def update_program_draft_lesson_title(self, **kwargs: Any) -> tuple[Any, Any]:
        lesson = self._lesson(kwargs["lesson_id"])
        lesson.title = kwargs["title"]
        self.calls.append(("title", kwargs))
        return self.record(), lesson

    def replace_program_draft_lesson_content(self, **kwargs: Any) -> tuple[Any, Any]:
        lesson = self._lesson(kwargs["lesson_id"])
        raw_kind = kwargs["content_kind"]
        lesson.content_kind = raw_kind if isinstance(raw_kind, ContentKind) else ContentKind(raw_kind)
        lesson.content_ref = kwargs["content_ref"]
        self.calls.append(("content", kwargs))
        return self.record(), lesson

    def move_program_draft_lesson(self, **kwargs: Any) -> Any:
        lesson = self._lesson(kwargs["lesson_id"])
        index = self.lessons.index(lesson)
        neighbor = index - 1 if kwargs["direction"] == "up" else index + 1
        self.lessons[index], self.lessons[neighbor] = self.lessons[neighbor], self.lessons[index]
        for position, item in enumerate(self.lessons, start=1):
            item.position = position
        self.calls.append(("move", kwargs))
        return self.record()

    def archive_program_draft_lesson(self, **kwargs: Any) -> Any:
        lesson = self._lesson(kwargs["lesson_id"])
        self.lessons.remove(lesson)
        for position, item in enumerate(self.lessons, start=1):
            item.position = position
        self.calls.append(("archive", kwargs))
        return self.record()


def install_store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    lesson_count: int = 10,
) -> tuple[FakeLessonStore, object]:
    store = FakeLessonStore(lesson_count)
    actor = object()

    async def fake_actor(_user_id: int, business_id: str) -> object:
        assert business_id == store.business_id
        return actor

    monkeypatch.setattr(editor.control, "_actor", fake_actor)
    for name in (
        "get_program_draft",
        "get_program_draft_lesson",
        "update_program_draft_lesson_title",
        "replace_program_draft_lesson_content",
        "move_program_draft_lesson",
        "archive_program_draft_lesson",
    ):
        monkeypatch.setattr(editor, name, getattr(store, name))
    return store, actor


@pytest.fixture(autouse=True)
def patch_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(editor.control, "Message", FakeMessage)
    monkeypatch.setattr(editor.asyncio, "to_thread", direct_to_thread)


def button_rows(message: FakeMessage) -> list[list[Any]]:
    return message.answers[-1][1]["reply_markup"].inline_keyboard


@pytest.mark.asyncio
async def test_lesson_list_is_paginated_and_callbacks_fit_telegram_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _actor = install_store(monkeypatch, lesson_count=10)
    callback = FakeCallback(
        editor._program_callback("dless", store.business_id, store.program_id, 0)
    )

    await editor.open_lesson_list(callback, FakeState())

    rows = button_rows(callback.message)
    lesson_buttons = [row[0] for row in rows[:8]]
    assert [button.text for button in lesson_buttons] == [
        f"{index}. Урок {index}" for index in range(1, 9)
    ]
    assert rows[8][0].text == "Дальше →"
    assert rows[-1][0].text == "К черновику"
    all_callbacks = [button.callback_data for row in rows for button in row]
    assert all(len(value.encode("utf-8")) <= 64 for value in all_callbacks)
    assert "Страница 1 из 2" in callback.message.answers[-1][0]

    second = FakeCallback(rows[8][0].callback_data)
    await editor.open_lesson_list(second, FakeState())
    second_rows = button_rows(second.message)
    assert [row[0].text for row in second_rows[:2]] == ["9. Урок 9", "10. Урок 10"]
    assert any(button.text == "← Назад" for row in second_rows for button in row)


@pytest.mark.asyncio
async def test_title_and_material_edits_are_saved_after_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, actor = install_store(monkeypatch, lesson_count=2)
    lesson = store.lessons[0]
    rename = FakeCallback(editor._lesson_callback("dlname", store.business_id, lesson.id))
    state = FakeState()

    await editor.begin_lesson_title_edit(rename, state)
    assert state.data == {
        "editor_business_id": store.business_id,
        "editor_lesson_id": lesson.id,
    }
    assert state.states[-1] == editor.ClientPlatformDraftLessonEditorState.title

    invalid = FakeMessage(text="   ")
    await editor.save_lesson_title(invalid, state)
    assert lesson.title == "Урок 1"
    assert "от 1 до 200" in invalid.answers[-1][0]

    valid = FakeMessage(text="  Новое   название  ")
    await editor.save_lesson_title(valid, state)
    assert lesson.title == "Новое название"
    assert state.clear_count == 2
    title_call = next(kwargs for name, kwargs in store.calls if name == "title")
    assert title_call["actor"] is actor

    replace = FakeCallback(editor._lesson_callback("dlmat", store.business_id, lesson.id))
    content_state = FakeState()
    await editor.begin_lesson_content_edit(replace, content_state)
    unsupported = FakeMessage(text="")
    await editor.save_lesson_content(unsupported, content_state)
    assert lesson.content_kind == ContentKind.TEXT
    assert "Поддерживаются" in unsupported.answers[-1][0]

    audio = FakeMessage(text=None)
    audio.audio = SimpleNamespace(file_id="telegram-audio-id")
    await editor.save_lesson_content(audio, content_state)
    assert lesson.content_kind == ContentKind.AUDIO
    assert lesson.content_ref == "telegram-audio-id"
    assert content_state.clear_count == 2


@pytest.mark.asyncio
async def test_move_and_confirmed_delete_refresh_the_editor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _actor = install_store(monkeypatch, lesson_count=4)
    target = store.lessons[2]

    detail = FakeCallback(editor._lesson_callback("dled", store.business_id, target.id))
    await editor.open_lesson(detail, FakeState())
    labels = [button.text for row in button_rows(detail.message) for button in row]
    assert "⬆️ Выше" in labels
    assert "⬇️ Ниже" in labels

    moved = FakeCallback(editor._lesson_callback("dlup", store.business_id, target.id))
    await editor.move_lesson_up(moved, FakeState())
    assert [item.title for item in store.lessons] == ["Урок 1", "Урок 3", "Урок 2", "Урок 4"]
    assert "Урок 2 из 4" in moved.message.answers[-1][0]

    ask = FakeCallback(editor._lesson_callback("dlask", store.business_id, target.id))
    await editor.ask_lesson_delete(ask, FakeState())
    confirm_button = button_rows(ask.message)[0][0]
    assert confirm_button.text == "Да, удалить урок"
    assert confirm_button.callback_data.startswith("cp:dldel:")

    deleted = FakeCallback(confirm_button.callback_data)
    await editor.confirm_lesson_delete(deleted, FakeState())
    assert [item.title for item in store.lessons] == ["Урок 1", "Урок 2", "Урок 4"]
    assert [item.position for item in store.lessons] == [1, 2, 3]
    assert "Всего: 3" in deleted.message.answers[-1][0]


@pytest.mark.asyncio
async def test_stale_editor_state_and_cancel_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _actor = install_store(monkeypatch, lesson_count=1)
    stale_title = FakeMessage(text="Новое название")
    stale_state = FakeState()
    await editor.save_lesson_title(stale_title, stale_state)
    assert stale_state.clear_count == 1
    assert "Редактор был закрыт" in stale_title.answers[-1][0]

    stale_content = FakeMessage(text="Новый материал")
    stale_content_state = FakeState()
    await editor.save_lesson_content(stale_content, stale_content_state)
    assert stale_content_state.clear_count == 1
    assert "Редактор был закрыт" in stale_content.answers[-1][0]

    lesson = store.lessons[0]
    cancel = FakeCallback(editor._lesson_callback("dlcancel", store.business_id, lesson.id))
    cancel_state = FakeState({"editor_business_id": store.business_id, "editor_lesson_id": lesson.id})
    await editor.cancel_lesson_edit(cancel, cancel_state)
    assert cancel_state.clear_count == 1
    assert cancel.answers[-1][0][0] == "Изменение отменено"
    assert "Урок 1 из 1" in cancel.message.answers[-1][0]


def test_detail_text_hides_non_text_file_identifiers() -> None:
    store = FakeLessonStore(lesson_count=1)
    lesson = store.lessons[0]
    lesson.content_kind = ContentKind.DOCUMENT
    lesson.content_ref = "private-telegram-file-id"
    text = editor._lesson_detail_text(store.record(), lesson)
    assert "документ" in text
    assert "private-telegram-file-id" not in text

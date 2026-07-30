from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

import pytest

from clientplatform.domain.programs import ContentKind, ProgramStatus

builder = importlib.import_module("handlers.clientplatform_program_builder")


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self, *, user_id: int = 101, text: str | None = None) -> None:
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
    def __init__(
        self,
        data: str,
        *,
        user_id: int = 101,
        message: FakeMessage | None = None,
    ) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message or FakeMessage(user_id=user_id)
        self.answers: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((args, kwargs))


class FakeState:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = dict(data or {})
        self.states: list[Any] = []
        self.clear_count = 0

    async def set_state(self, value: Any) -> None:
        self.states.append(value)

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def clear(self) -> None:
        self.clear_count += 1
        self.data.clear()


class FakeDraftStore:
    def __init__(self, business_id: str) -> None:
        self.business_id = business_id
        self.records: dict[str, Any] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def create_program(self, **kwargs: Any) -> Any:
        program_id = str(uuid4())
        program = SimpleNamespace(
            id=program_id,
            business_id=self.business_id,
            title=kwargs["title"],
            status=ProgramStatus.DRAFT,
        )
        self.records[program_id] = SimpleNamespace(program=program, lessons=[])
        self.calls.append(("create", kwargs))
        return program

    def add_program_lesson(self, **kwargs: Any) -> Any:
        record = self.records[kwargs["program_id"]]
        lesson = SimpleNamespace(
            id=str(uuid4()),
            position=len(record.lessons) + 1,
            title=kwargs["title"],
            content_kind=ContentKind(str(kwargs["content_kind"])),
            content_ref=kwargs["content_ref"],
        )
        record.lessons.append(lesson)
        self.calls.append(("lesson", kwargs))
        return lesson

    def get_program_draft(self, **kwargs: Any) -> Any:
        self.calls.append(("get", kwargs))
        return self.records[kwargs["program_id"]]

    def list_program_drafts(self, **kwargs: Any) -> list[Any]:
        self.calls.append(("list_drafts", kwargs))
        return [
            record.program
            for record in self.records.values()
            if record.program.status == ProgramStatus.DRAFT
        ]

    def list_programs(self, **kwargs: Any) -> list[Any]:
        self.calls.append(("list", kwargs))
        return [
            record.program
            for record in self.records.values()
            if record.program.status != ProgramStatus.ARCHIVED
        ]

    def publish_program(self, **kwargs: Any) -> Any:
        record = self.records[kwargs["program_id"]]
        record.program.status = ProgramStatus.ACTIVE
        self.calls.append(("publish", kwargs))
        return record.program

    def archive_program_draft(self, **kwargs: Any) -> Any:
        record = self.records[kwargs["program_id"]]
        record.program.status = ProgramStatus.ARCHIVED
        self.calls.append(("archive", kwargs))
        return record.program


async def direct_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@pytest.fixture(autouse=True)
def patch_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builder.control, "Message", FakeMessage)
    monkeypatch.setattr(builder.asyncio, "to_thread", direct_to_thread)


def install_store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    business_id: str,
) -> tuple[FakeDraftStore, object, list[tuple[int, str]]]:
    store = FakeDraftStore(business_id)
    actor = object()
    actor_calls: list[tuple[int, str]] = []

    async def fake_actor(user_id: int, selected_business_id: str) -> object:
        actor_calls.append((user_id, selected_business_id))
        return actor

    monkeypatch.setattr(builder.control, "_actor", fake_actor)
    for name in (
        "create_program",
        "add_program_lesson",
        "get_program_draft",
        "list_program_drafts",
        "list_programs",
        "publish_program",
        "archive_program_draft",
    ):
        monkeypatch.setattr(builder, name, getattr(store, name))
    return store, actor, actor_calls


@pytest.mark.asyncio
async def test_persistent_multi_lesson_journey_resumes_after_fsm_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    business_token = builder.control._uuid_token(business_id)
    store, actor, _actor_calls = install_store(
        monkeypatch,
        business_id=business_id,
    )
    dashboard_calls: list[tuple[int, str]] = []

    async def fake_dashboard(
        _message: Any,
        *,
        user_id: int,
        business_id: str,
    ) -> None:
        dashboard_calls.append((user_id, business_id))

    monkeypatch.setattr(builder.control, "_send_dashboard", fake_dashboard)

    state = FakeState()
    await builder.begin_program(FakeCallback(f"cp:progadd:{business_token}"), state)
    assert state.data == {"business_id": business_id}

    title_message = FakeMessage(text="  Спокойный сон  ")
    await builder.capture_program_title(title_message, state)
    program_id = state.data["program_id"]
    assert store.records[program_id].program.title == "Спокойный сон"
    assert store.records[program_id].program.status == ProgramStatus.DRAFT
    assert "сохраняться автоматически" in title_message.answers[-1][0]

    await builder.capture_lesson_title(FakeMessage(text="Введение"), state)
    first = FakeMessage(text="Первый текст")
    await builder.capture_lesson_content(first, state)
    assert [item.title for item in store.records[program_id].lessons] == ["Введение"]
    assert [name for name, _kwargs in store.calls].count("lesson") == 1
    first_buttons = [
        (button.text, button.callback_data)
        for row in first.answers[-1][1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert [text for text, _data in first_buttons] == [
        "Добавить ещё урок",
        "Опубликовать программу",
        "Удалить черновик",
    ]
    assert all(len(data.encode("utf-8")) <= 64 for _text, data in first_buttons)

    restarted_state = FakeState()
    open_callback = FakeCallback(
        builder._program_callback("dopen", business_id, program_id)
    )
    await builder.open_draft(open_callback, restarted_state)
    assert restarted_state.data == {
        "business_id": business_id,
        "program_id": program_id,
    }
    assert "Уроков сохранено: 1" in open_callback.message.answers[-1][0]

    await builder.add_lesson(
        FakeCallback(builder._program_callback("dadd", business_id, program_id)),
        restarted_state,
    )
    await builder.capture_lesson_title(FakeMessage(text="Практика"), restarted_state)
    second = FakeMessage(text=None)
    second.audio = SimpleNamespace(file_id="telegram-audio-id")
    await builder.capture_lesson_content(second, restarted_state)
    assert [item.title for item in store.records[program_id].lessons] == [
        "Введение",
        "Практика",
    ]

    publish = FakeCallback(
        builder._program_callback("dpub", business_id, program_id)
    )
    await builder.publish_draft(publish, FakeState())
    assert store.records[program_id].program.status == ProgramStatus.ACTIVE
    assert dashboard_calls == [(101, business_id)]
    assert "Уроков: 2" in publish.message.answers[-1][0]
    publish_call = next(kwargs for name, kwargs in store.calls if name == "publish")
    assert publish_call["actor"] is actor


@pytest.mark.asyncio
async def test_program_screen_and_delivery_hide_drafts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    business_token = builder.control._uuid_token(business_id)
    store, _actor, _actor_calls = install_store(
        monkeypatch,
        business_id=business_id,
    )
    draft = store.create_program(title="Черновик")
    active = store.create_program(title="Опубликованная")
    store.records[active.id].program.status = ProgramStatus.ACTIVE

    programs = FakeCallback(f"cp:cap:{business_token}:programs")
    await builder.open_programs(programs, FakeState())
    text, kwargs = programs.message.answers[-1]
    assert "📝 Черновик" in text
    assert "✅ Опубликованная" in text
    buttons = [
        (button.text, button.callback_data)
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert any(text == "Черновики · 1" for text, _data in buttons)
    assert any(text == "Выдать клиенту" for text, _data in buttons)

    delivery = FakeCallback(f"cp:deliver:{business_token}")
    await builder.choose_active_program_for_delivery(delivery, FakeState())
    delivery_buttons = [
        button.text
        for row in delivery.message.answers[-1][1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert delivery_buttons == ["Опубликованная"]
    assert draft.title not in delivery_buttons


@pytest.mark.asyncio
async def test_archive_and_obsolete_callbacks_fail_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    store, _actor, _actor_calls = install_store(
        monkeypatch,
        business_id=business_id,
    )
    program = store.create_program(title="Черновик")
    dashboards: list[str] = []

    async def fake_dashboard(_message: Any, **kwargs: Any) -> None:
        dashboards.append(kwargs["business_id"])

    monkeypatch.setattr(builder.control, "_send_dashboard", fake_dashboard)

    archived = FakeCallback(
        builder._program_callback("darc", business_id, program.id)
    )
    await builder.archive_draft(archived, FakeState())
    assert store.records[program.id].program.status == ProgramStatus.ARCHIVED
    assert dashboards == [business_id]
    assert "удалён" in archived.message.answers[-1][0]

    obsolete = FakeCallback("cp:pbuild:publish")
    await builder.obsolete_builder_callback(obsolete)
    assert obsolete.answers[-1][1]["show_alert"] is True
    assert "устарела" in obsolete.answers[-1][0][0]


@pytest.mark.asyncio
async def test_builder_validates_titles_materials_and_lesson_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    store, _actor, _actor_calls = install_store(
        monkeypatch,
        business_id=business_id,
    )
    invalid_title = FakeMessage(text="   ")
    state = FakeState({"business_id": business_id})
    await builder.capture_program_title(invalid_title, state)
    assert not store.records
    assert "от 1 до 200" in invalid_title.answers[-1][0]

    program = store.create_program(title="Программа")
    invalid_lesson = FakeMessage(text="x" * 201)
    lesson_state = FakeState(
        {"business_id": business_id, "program_id": program.id}
    )
    await builder.capture_lesson_title(invalid_lesson, lesson_state)
    assert "от 1 до 200" in invalid_lesson.answers[-1][0]

    unsupported = FakeMessage(text="")
    content_state = FakeState(
        {
            "business_id": business_id,
            "program_id": program.id,
            "lesson_title": "Урок",
        }
    )
    await builder.capture_lesson_content(unsupported, content_state)
    assert store.records[program.id].lessons == []
    assert "Поддерживаются" in unsupported.answers[-1][0]

    for position in range(100):
        store.records[program.id].lessons.append(
            SimpleNamespace(
                position=position + 1,
                title=f"Урок {position + 1}",
                content_kind=ContentKind.TEXT,
            )
        )
    full = FakeCallback(builder._program_callback("dadd", business_id, program.id))
    await builder.add_lesson(full, FakeState())
    assert full.answers[-1][1]["show_alert"] is True
    assert "100 уроков" in full.answers[-1][0][0]


def test_review_text_is_bounded_for_large_program() -> None:
    record = SimpleNamespace(
        program=SimpleNamespace(title="Программа"),
        lessons=[
            SimpleNamespace(
                position=index + 1,
                title=f"Урок {index} " + ("x" * 200),
                content_kind=ContentKind.TEXT,
            )
            for index in range(100)
        ],
    )
    text = builder._review_text(record)
    assert len(text) < 4096
    assert "…и ещё 80 уроков" in text

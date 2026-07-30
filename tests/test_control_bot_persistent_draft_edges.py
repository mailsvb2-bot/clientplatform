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


@pytest.fixture(autouse=True)
def patch_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builder.control, "Message", FakeMessage)
    monkeypatch.setattr(builder.asyncio, "to_thread", direct_to_thread)


async def install_actor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    business_id: str,
) -> object:
    actor = object()

    async def fake_actor(_user_id: int, selected_business_id: str) -> object:
        assert selected_business_id == business_id
        return actor

    monkeypatch.setattr(builder.control, "_actor", fake_actor)
    return actor


def draft_record(*, business_id: str, lessons: int) -> Any:
    return SimpleNamespace(
        program=SimpleNamespace(
            id=str(uuid4()),
            business_id=business_id,
            title="Черновик",
            status=ProgramStatus.DRAFT,
        ),
        lessons=tuple(
            SimpleNamespace(
                id=str(uuid4()),
                position=index + 1,
                title=f"Урок {index + 1}",
                content_kind=ContentKind.TEXT,
                content_ref="Материал",
            )
            for index in range(lessons)
        ),
    )


def test_helpers_cover_empty_and_capacity_branches() -> None:
    business_id = str(uuid4())
    full = draft_record(business_id=business_id, lessons=100)

    assert builder._session_ids({}) is None
    assert builder._program_lines([]) == "Пока нет программ."

    empty_keyboard = builder._programs_keyboard(
        business_id=business_id,
        drafts=[],
        active=[],
    )
    empty_labels = [
        button.text
        for row in empty_keyboard.inline_keyboard
        for button in row
    ]
    assert empty_labels == ["Создать программу"]

    full_labels = [
        button.text
        for row in builder._draft_keyboard(full).inline_keyboard
        for button in row
    ]
    assert full_labels == ["Опубликовать программу", "Удалить черновик"]


@pytest.mark.asyncio
async def test_empty_draft_and_delivery_menus_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = builder.control._uuid_token(business_id)
    await install_actor(monkeypatch, business_id=business_id)
    monkeypatch.setattr(builder, "list_programs", lambda **_kwargs: [])
    monkeypatch.setattr(builder, "list_program_drafts", lambda **_kwargs: [])

    delivery = FakeCallback(f"cp:deliver:{token}")
    delivery_state = FakeState()
    await builder.choose_active_program_for_delivery(delivery, delivery_state)
    assert delivery_state.data == {"business_id": business_id}
    assert "Сначала опубликуйте" in delivery.message.answers[-1][0]

    drafts = FakeCallback(f"cp:drafts:{token}")
    draft_state = FakeState({"stale": "value"})
    await builder.open_drafts(drafts, draft_state)
    assert draft_state.clear_count == 1
    assert "Сохранённых черновиков нет" in drafts.message.answers[-1][0]


@pytest.mark.asyncio
async def test_nonempty_draft_menu_uses_self_contained_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = builder.control._uuid_token(business_id)
    await install_actor(monkeypatch, business_id=business_id)
    first = SimpleNamespace(id=str(uuid4()), title="Очень длинный " + ("А" * 80))
    second = SimpleNamespace(id=str(uuid4()), title="Второй")
    monkeypatch.setattr(
        builder,
        "list_program_drafts",
        lambda **_kwargs: [first, second],
    )

    callback = FakeCallback(f"cp:drafts:{token}")
    await builder.open_drafts(callback, FakeState())
    buttons = [
        button
        for row in callback.message.answers[-1][1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert [button.text for button in buttons] == [first.title[:42], "Второй"]
    assert all(button.callback_data.startswith("cp:dopen:") for button in buttons)
    assert all(len(button.callback_data.encode("utf-8")) <= 64 for button in buttons)


@pytest.mark.asyncio
async def test_corrupted_fsm_and_invalid_titles_do_not_mutate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        builder,
        "create_program",
        lambda **kwargs: create_calls.append(kwargs),
    )

    missing_business = FakeMessage(text="Название")
    missing_business_state = FakeState()
    await builder.capture_program_title(missing_business, missing_business_state)
    assert missing_business_state.clear_count == 1
    assert "Конструктор был закрыт" in missing_business.answers[-1][0]

    business_id = str(uuid4())
    await install_actor(monkeypatch, business_id=business_id)
    invalid_title = FakeMessage(text="   ")
    await builder.capture_program_title(
        invalid_title,
        FakeState({"business_id": business_id}),
    )
    assert create_calls == []
    assert "от 1 до 200" in invalid_title.answers[-1][0]

    missing_program = FakeMessage(text="Урок")
    missing_program_state = FakeState({"business_id": business_id})
    await builder.capture_lesson_title(missing_program, missing_program_state)
    assert missing_program_state.clear_count == 1
    assert "Конструктор был закрыт" in missing_program.answers[-1][0]

    invalid_lesson = FakeMessage(text="x" * 201)
    await builder.capture_lesson_title(
        invalid_lesson,
        FakeState({"business_id": business_id, "program_id": str(uuid4())}),
    )
    assert "от 1 до 200" in invalid_lesson.answers[-1][0]


@pytest.mark.asyncio
async def test_lesson_content_edges_and_empty_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    program_id = str(uuid4())
    actor = await install_actor(monkeypatch, business_id=business_id)

    missing_title = FakeMessage(text="Материал")
    missing_state = FakeState(
        {"business_id": business_id, "program_id": program_id}
    )
    await builder.capture_lesson_content(missing_title, missing_state)
    assert missing_state.clear_count == 1
    assert "Конструктор был закрыт" in missing_title.answers[-1][0]

    full_record = draft_record(business_id=business_id, lessons=100)

    async def load_full(**_kwargs: Any) -> Any:
        return full_record

    monkeypatch.setattr(builder, "_load_draft", load_full)
    at_limit = FakeMessage(text="Материал")
    limit_state = FakeState(
        {
            "business_id": business_id,
            "program_id": full_record.program.id,
            "lesson_title": "Урок 101",
        }
    )
    await builder.capture_lesson_content(at_limit, limit_state)
    assert limit_state.states[-1] == builder.ClientPlatformProgramBuilderState.review
    assert "не более 100" in at_limit.answers[-1][0]

    empty_record = draft_record(business_id=business_id, lessons=0)

    async def load_empty(**_kwargs: Any) -> Any:
        return empty_record

    monkeypatch.setattr(builder, "_load_draft", load_empty)
    unsupported = FakeMessage(text="")
    unsupported_state = FakeState(
        {
            "business_id": business_id,
            "program_id": empty_record.program.id,
            "lesson_title": "Урок",
        }
    )
    await builder.capture_lesson_content(unsupported, unsupported_state)
    assert "Поддерживаются" in unsupported.answers[-1][0]

    monkeypatch.setattr(
        builder,
        "get_program_draft",
        lambda **_kwargs: empty_record,
    )
    monkeypatch.setattr(
        builder,
        "publish_program",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("empty draft must not publish")
        ),
    )
    publish = FakeCallback(
        builder._program_callback("dpub", business_id, empty_record.program.id)
    )
    await builder.publish_draft(publish, FakeState())
    assert publish.answers[-1][1]["show_alert"] is True
    assert "Добавьте хотя бы один урок" in publish.answers[-1][0][0]
    assert actor is not None

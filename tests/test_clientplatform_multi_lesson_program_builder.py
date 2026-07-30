from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

import pytest

from clientplatform.domain.programs import ContentKind

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


async def direct_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@pytest.fixture(autouse=True)
def patch_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builder.control, "Message", FakeMessage)
    monkeypatch.setattr(builder.asyncio, "to_thread", direct_to_thread)


@pytest.mark.asyncio
async def test_multi_lesson_journey_writes_only_on_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    business_token = builder.control._uuid_token(business_id)
    actor = object()
    actor_calls: list[tuple[int, str]] = []

    async def fake_actor(user_id: int, selected_business_id: str) -> object:
        actor_calls.append((user_id, selected_business_id))
        return actor

    monkeypatch.setattr(builder.control, "_actor", fake_actor)

    published: list[dict[str, Any]] = []

    def fake_publish(**kwargs: Any) -> Any:
        published.append(kwargs)
        lesson_rows = tuple(
            SimpleNamespace(title=lesson.title)
            for lesson in kwargs["lessons"]
        )
        return SimpleNamespace(
            program=SimpleNamespace(title=kwargs["program_title"]),
            lessons=lesson_rows,
        )

    monkeypatch.setattr(builder, "create_multi_lesson_program", fake_publish)
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
    start = FakeCallback(f"cp:progadd:{business_token}")
    await builder.begin_program(start, state)
    assert state.data == {"business_id": business_id, "lessons": []}
    assert state.states[-1] == builder.ClientPlatformProgramBuilderState.program_title
    assert published == []

    await builder.capture_program_title(FakeMessage(text="  Спокойный сон  "), state)
    assert state.data["program_title"] == "Спокойный сон"
    assert state.states[-1] == builder.ClientPlatformProgramBuilderState.lesson_title

    await builder.capture_lesson_title(FakeMessage(text="Введение"), state)
    first = FakeMessage(text="Первый текст")
    await builder.capture_lesson_content(first, state)
    assert state.states[-1] == builder.ClientPlatformProgramBuilderState.review
    assert state.data["lessons"] == [
        {
            "title": "Введение",
            "content_kind": ContentKind.TEXT.value,
            "content_ref": "Первый текст",
        }
    ]
    first_buttons = [
        button.text
        for row in first.answers[-1][1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert first_buttons == [
        "Добавить ещё урок",
        "Опубликовать программу",
        "Отменить создание",
    ]
    assert published == []

    await builder.add_lesson(FakeCallback("cp:pbuild:add"), state)
    assert state.states[-1] == builder.ClientPlatformProgramBuilderState.lesson_title
    await builder.capture_lesson_title(FakeMessage(text="Практика"), state)
    second = FakeMessage(text=None)
    second.audio = SimpleNamespace(file_id="telegram-audio-id")
    await builder.capture_lesson_content(second, state)
    assert [item["title"] for item in state.data["lessons"]] == [
        "Введение",
        "Практика",
    ]
    assert published == []

    publish = FakeCallback("cp:pbuild:publish")
    await builder.publish_program(publish, state)
    assert len(published) == 1
    assert published[0]["actor"] is actor
    assert published[0]["program_title"] == "Спокойный сон"
    assert [lesson.title for lesson in published[0]["lessons"]] == [
        "Введение",
        "Практика",
    ]
    assert [lesson.content_kind for lesson in published[0]["lessons"]] == [
        ContentKind.TEXT.value,
        ContentKind.AUDIO.value,
    ]
    assert state.clear_count == 2
    assert dashboard_calls == [(101, business_id)]
    assert "Уроков: 2" in publish.message.answers[-1][0]
    assert actor_calls == [
        (101, business_id),
        (101, business_id),
        (101, business_id),
    ]


@pytest.mark.asyncio
async def test_cancel_and_stale_callbacks_do_not_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())

    async def fake_actor(_user_id: int, _business_id: str) -> object:
        return object()

    monkeypatch.setattr(builder.control, "_actor", fake_actor)
    monkeypatch.setattr(
        builder,
        "create_multi_lesson_program",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not publish")),
    )

    dashboards: list[str] = []

    async def fake_dashboard(_message: Any, **kwargs: Any) -> None:
        dashboards.append(kwargs["business_id"])

    monkeypatch.setattr(builder.control, "_send_dashboard", fake_dashboard)

    state = FakeState(
        {
            "business_id": business_id,
            "program_title": "Черновик",
            "lessons": [
                {
                    "title": "Урок",
                    "content_kind": ContentKind.TEXT.value,
                    "content_ref": "Текст",
                }
            ],
        }
    )
    cancelled = FakeCallback("cp:pbuild:cancel")
    await builder.cancel_program(cancelled, state)
    assert state.clear_count == 1
    assert dashboards == [business_id]
    assert "ничего не создавалось" in cancelled.message.answers[-1][0]

    stale = FakeCallback("cp:pbuild:publish")
    await builder.publish_program(stale, FakeState())
    assert stale.answers[-1][1]["show_alert"] is True
    assert "Конструктор уже закрыт" in stale.answers[-1][0][0]


@pytest.mark.asyncio
async def test_builder_validates_titles_materials_and_lesson_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_title = FakeMessage(text="   ")
    state = FakeState({"business_id": str(uuid4()), "lessons": []})
    await builder.capture_program_title(invalid_title, state)
    assert state.states == []
    assert "от 1 до 200" in invalid_title.answers[-1][0]

    invalid_lesson = FakeMessage(text="x" * 201)
    await builder.capture_lesson_title(invalid_lesson, state)
    assert state.states == []
    assert "от 1 до 200" in invalid_lesson.answers[-1][0]

    unsupported = FakeMessage(text="")
    content_state = FakeState(
        {
            "business_id": str(uuid4()),
            "program_title": "Программа",
            "lesson_title": "Урок",
            "lessons": [],
        }
    )
    await builder.capture_lesson_content(unsupported, content_state)
    assert content_state.data["lessons"] == []
    assert "Поддерживаются" in unsupported.answers[-1][0]

    business_id = str(uuid4())

    async def fake_actor(_user_id: int, _business_id: str) -> object:
        return object()

    monkeypatch.setattr(builder.control, "_actor", fake_actor)
    full_state = FakeState(
        {
            "business_id": business_id,
            "program_title": "Большая программа",
            "lessons": [
                {
                    "title": f"Урок {index}",
                    "content_kind": ContentKind.TEXT.value,
                    "content_ref": "Текст",
                }
                for index in range(100)
            ],
        }
    )
    full = FakeCallback("cp:pbuild:add")
    await builder.add_lesson(full, full_state)
    assert full.answers[-1][1]["show_alert"] is True
    assert "100 уроков" in full.answers[-1][0][0]


def test_review_text_is_bounded_for_large_program() -> None:
    lessons = [
        {
            "title": f"Урок {index} " + ("x" * 200),
            "content_kind": ContentKind.TEXT.value,
            "content_ref": "Текст",
        }
        for index in range(100)
    ]
    text = builder._review_text(program_title="Программа", lessons=lessons)
    assert len(text) < 4096
    assert "…и ещё 80 уроков" in text

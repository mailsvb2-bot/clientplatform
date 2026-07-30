from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

import pytest

from clientplatform.domain.programs import ContentKind
from handlers.clientplatform_program_media import ProgramMediaIngestError

media_router = importlib.import_module("handlers.clientplatform_program_media_router")


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self, *, user_id: int = 101) -> None:
        self.from_user = FakeUser(user_id)
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class FakeState:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = dict(data)
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


async def direct_to_thread(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    return func(*args, **kwargs)


@pytest.fixture(autouse=True)
def direct_thread_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media_router.asyncio, "to_thread", direct_to_thread)


@pytest.mark.asyncio
async def test_builder_persists_only_externalized_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    program_id = str(uuid4())
    actor = object()
    initial = SimpleNamespace(lessons=())
    updated = SimpleNamespace(lessons=(SimpleNamespace(id=str(uuid4())),))
    writes: list[dict[str, Any]] = []
    reviews: list[Any] = []

    async def materialize(_message: Any, *, business_id: str) -> tuple[Any, str]:
        assert business_id
        return ContentKind.AUDIO, "s3://clientplatform-production/program-media/audio.ogg"

    async def load_draft(**_kwargs: Any) -> Any:
        return initial

    async def resolve_actor(_user_id: int, selected_business_id: str) -> object:
        assert selected_business_id == business_id
        return actor

    def add_lesson(**kwargs: Any) -> None:
        writes.append(kwargs)

    def get_draft(**_kwargs: Any) -> Any:
        return updated

    async def send_review(_message: Any, record: Any) -> None:
        reviews.append(record)

    monkeypatch.setattr(media_router, "materialize_program_content", materialize)
    monkeypatch.setattr(media_router.builder, "_load_draft", load_draft)
    monkeypatch.setattr(media_router.control, "_actor", resolve_actor)
    monkeypatch.setattr(media_router.builder, "add_program_lesson", add_lesson)
    monkeypatch.setattr(media_router.builder, "get_program_draft", get_draft)
    monkeypatch.setattr(media_router.builder, "_send_draft_review", send_review)

    state = FakeState(
        {
            "business_id": business_id,
            "program_id": program_id,
            "lesson_title": "Аудиоурок",
        }
    )
    await media_router.capture_persistent_lesson_content(FakeMessage(), state)

    assert len(writes) == 1
    assert writes[0]["actor"] is actor
    assert writes[0]["content_kind"] == ContentKind.AUDIO
    assert writes[0]["content_ref"].startswith("s3://")
    assert "control-bot" not in writes[0]["content_ref"]
    assert reviews == [updated]
    assert state.data["lesson_title"] == ""


@pytest.mark.asyncio
async def test_ingest_failure_never_mutates_builder_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    program_id = str(uuid4())
    writes: list[dict[str, Any]] = []

    async def fail_ingest(_message: Any, *, business_id: str) -> tuple[Any, str]:
        assert business_id
        raise ProgramMediaIngestError(
            "program_media_upload_transport_failure",
            retryable=True,
        )

    async def load_draft(**_kwargs: Any) -> Any:
        return SimpleNamespace(lessons=())

    monkeypatch.setattr(media_router, "materialize_program_content", fail_ingest)
    monkeypatch.setattr(media_router.builder, "_load_draft", load_draft)
    monkeypatch.setattr(
        media_router.builder,
        "add_program_lesson",
        lambda **kwargs: writes.append(kwargs),
    )

    message = FakeMessage()
    state = FakeState(
        {
            "business_id": business_id,
            "program_id": program_id,
            "lesson_title": "Документ",
        }
    )
    await media_router.capture_persistent_lesson_content(message, state)

    assert writes == []
    assert state.data["lesson_title"] == "Документ"
    assert "Попробуйте отправить его ещё раз" in message.answers[-1][0]


@pytest.mark.asyncio
async def test_editor_replaces_material_only_after_externalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    lesson_id = str(uuid4())
    actor = object()
    writes: list[dict[str, Any]] = []
    detail_calls: list[tuple[Any, Any]] = []
    record = SimpleNamespace(program=SimpleNamespace(title="Черновик"), lessons=())
    lesson = SimpleNamespace(id=lesson_id)

    async def materialize(_message: Any, *, business_id: str) -> tuple[Any, str]:
        assert business_id
        return ContentKind.DOCUMENT, "s3://clientplatform-production/program-media/file.pdf"

    async def resolve_actor(_user_id: int, selected_business_id: str) -> object:
        assert selected_business_id == business_id
        return actor

    def replace(**kwargs: Any) -> tuple[Any, Any]:
        writes.append(kwargs)
        return record, lesson

    async def send_detail(_message: Any, *, record: Any, lesson: Any) -> None:
        detail_calls.append((record, lesson))

    monkeypatch.setattr(media_router, "materialize_program_content", materialize)
    monkeypatch.setattr(media_router.control, "_actor", resolve_actor)
    monkeypatch.setattr(
        media_router.editor,
        "replace_program_draft_lesson_content",
        replace,
    )
    monkeypatch.setattr(media_router.editor, "_send_lesson_detail", send_detail)

    state = FakeState(
        {
            "editor_business_id": business_id,
            "editor_lesson_id": lesson_id,
        }
    )
    await media_router.replace_persistent_lesson_content(FakeMessage(), state)

    assert writes[0]["content_ref"].startswith("s3://")
    assert writes[0]["lesson_id"] == lesson_id
    assert state.clear_count == 1
    assert detail_calls == [(record, lesson)]


def test_media_router_is_composed_before_all_program_handlers() -> None:
    handlers = importlib.import_module("handlers")
    control = handlers.clientplatform_control
    assert control.router.name == "clientplatform_media_entry"
    assert control.router.sub_routers[0].name == "clientplatform_program_media_router"
    legacy_entry = control.router.sub_routers[1]
    assert legacy_entry.name == "clientplatform_entry"

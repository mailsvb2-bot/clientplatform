from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from clientplatform.domain.programs import ContentKind

cloud = importlib.import_module("handlers.clientplatform_cloud_media")
control = importlib.import_module("handlers.clientplatform_control")
builder = importlib.import_module("handlers.clientplatform_program_builder")
editor = importlib.import_module("handlers.clientplatform_program_lesson_editor")


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.from_user = FakeUser()
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = FakeUser()
        self.message = FakeMessage()
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
def direct_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(control, "Message", FakeMessage)
    monkeypatch.setattr(control, "_actor", AsyncMock(return_value=object()))


def _add_state() -> FakeState:
    business_id = str(uuid4())
    program_id = str(uuid4())
    return FakeState(
        {
            "business_id": business_id,
            "program_id": program_id,
            "lesson_title": "Урок",
            "cloud_media_mode": "add",
            "cloud_media_business_id": business_id,
            "cloud_media_target_id": program_id,
            "cloud_media_lesson_title": "Урок",
            "cloud_media_kind": "video",
        }
    )


def test_cloud_keyboards_explain_storage_choice() -> None:
    kinds = [button.text for row in cloud._kind_keyboard().inline_keyboard for button in row]
    assert "🎬 Видео" in kinds
    sources = [button.text for row in cloud._source_keyboard().inline_keyboard for button in row]
    assert sources[0].startswith("☁️ В облаке")
    urls = [button.url for row in cloud._cloud_help_keyboard().inline_keyboard for button in row]
    assert any("yandex" in str(url) for url in urls)
    assert any("drive.google" in str(url) for url in urls)


@pytest.mark.asyncio
async def test_lesson_title_enters_cloud_first_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    program_id = str(uuid4())
    monkeypatch.setattr(builder, "_session_ids", lambda _data: (business_id, program_id))
    state = FakeState({"business_id": business_id, "program_id": program_id})
    message = FakeMessage("Первый урок")
    await cloud.capture_lesson_title_cloud_first(message, state)
    assert state.states[-1] == cloud.ClientPlatformCloudMediaState.choose_kind
    assert state.data["cloud_media_mode"] == "add"
    assert "без расхода места" in message.answers[-1][0]


@pytest.mark.asyncio
async def test_replacement_starts_same_source_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    lesson_id = str(uuid4())
    monkeypatch.setattr(
        editor,
        "_parse_lesson_callback",
        lambda _callback: (business_id, lesson_id),
    )
    monkeypatch.setattr(
        editor,
        "_load_lesson",
        AsyncMock(return_value=(object(), object(), SimpleNamespace(title="Видео"))),
    )
    callback = FakeCallback("cp:dlmat:x:y")
    state = FakeState()
    await cloud.begin_cloud_first_replacement(callback, state)
    assert state.data["cloud_media_mode"] == "replace"
    assert state.data["cloud_media_target_id"] == lesson_id


@pytest.mark.asyncio
async def test_kind_and_source_selection_routes_to_existing_upload_states() -> None:
    state = _add_state()
    callback = FakeCallback("cpcm:k:video")
    await cloud.choose_material_kind(callback, state)
    assert state.states[-1] == cloud.ClientPlatformCloudMediaState.choose_source

    device = FakeCallback("cpcm:s:device")
    await cloud.choose_device_source(device, state)
    assert state.states[-1] == builder.ClientPlatformProgramBuilderState.lesson_content
    assert "защищённом хранилище" in device.message.answers[-1][0]


@pytest.mark.asyncio
async def test_cloud_source_explains_public_share_flow() -> None:
    state = _add_state()
    callback = FakeCallback("cpcm:s:cloud")
    await cloud.choose_cloud_source(callback, state)
    assert state.states[-1] == cloud.ClientPlatformCloudMediaState.public_url
    text, kwargs = callback.message.answers[-1]
    assert "доступ «по ссылке»" in text
    assert len(kwargs["reply_markup"].inline_keyboard) == 4


@pytest.mark.asyncio
async def test_public_cloud_url_adds_without_server_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _add_state()
    added: list[dict[str, Any]] = []
    monkeypatch.setattr(cloud, "add_program_lesson", lambda **kwargs: added.append(kwargs))
    record = SimpleNamespace(program=SimpleNamespace(title="Программа"), lessons=[])
    monkeypatch.setattr(cloud, "get_program_draft", lambda **_kwargs: record)
    review = AsyncMock()
    monkeypatch.setattr(builder, "_send_draft_review", review)

    message = FakeMessage("https://disk.yandex.ru/d/public-file")
    await cloud.save_public_cloud_url(message, state)

    assert added[0]["content_kind"] == ContentKind.VIDEO
    assert added[0]["content_ref"] == "https://disk.yandex.ru/d/public-file"
    assert "не копировался" in message.answers[-1][0]
    review.assert_awaited_once_with(message, record)


@pytest.mark.asyncio
async def test_streaming_video_becomes_safe_link(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _add_state()
    added: list[dict[str, Any]] = []
    monkeypatch.setattr(cloud, "add_program_lesson", lambda **kwargs: added.append(kwargs))
    monkeypatch.setattr(
        cloud,
        "get_program_draft",
        lambda **_kwargs: SimpleNamespace(program=SimpleNamespace(title="Программа"), lessons=[]),
    )
    monkeypatch.setattr(builder, "_send_draft_review", AsyncMock())
    message = FakeMessage("https://youtu.be/abcdefghijk")
    await cloud.save_public_cloud_url(message, state)
    assert added[0]["content_kind"] == ContentKind.LINK
    assert "видеосервис" in message.answers[-1][0]


@pytest.mark.asyncio
async def test_invalid_cloud_url_keeps_editor_open() -> None:
    state = _add_state()
    message = FakeMessage("http://localhost/file.mp4")
    await cloud.save_public_cloud_url(message, state)
    assert "Не получилось принять ссылку" in message.answers[-1][0]
    assert state.clear_count == 0


@pytest.mark.asyncio
async def test_public_cloud_url_replaces_and_cleans_old_private_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    lesson_id = str(uuid4())
    state = FakeState(
        {
            "cloud_media_mode": "replace",
            "cloud_media_business_id": business_id,
            "cloud_media_target_id": lesson_id,
            "cloud_media_lesson_title": "Видео",
            "cloud_media_kind": "video",
        }
    )
    old = SimpleNamespace(content_ref="s3://bucket/program-media/old/video.mp4")
    monkeypatch.setattr(
        editor,
        "get_program_draft_lesson",
        lambda **_kwargs: (object(), old),
    )
    record = SimpleNamespace(program=SimpleNamespace(title="Программа"), lessons=[])
    lesson = SimpleNamespace(
        content_ref="https://www.dropbox.com/s/demo/video.mp4?dl=0",
        content_kind=ContentKind.VIDEO,
    )
    monkeypatch.setattr(
        cloud,
        "replace_program_draft_lesson_content",
        lambda **_kwargs: (record, lesson),
    )
    cleanups: list[dict[str, Any]] = []
    monkeypatch.setattr(cloud, "queue_program_media_cleanup", lambda **kwargs: cleanups.append(kwargs))
    detail = AsyncMock()
    monkeypatch.setattr(editor, "_send_lesson_detail", detail)

    message = FakeMessage("https://www.dropbox.com/s/demo/video.mp4?dl=0")
    await cloud.save_public_cloud_url(message, state)

    assert cleanups[0]["media_reference"].startswith("s3://")
    assert "Dropbox" in message.answers[-1][0]
    detail.assert_awaited_once()

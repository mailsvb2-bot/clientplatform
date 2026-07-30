from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest

from clientplatform.domain.programs import ContentKind

builder = importlib.import_module("handlers.clientplatform_program_builder")


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.from_user = type("User", (), {"id": 101})()
        self.text = text
        self.audio = None
        self.voice = None
        self.video = None
        self.document = None
        self.photo: list[Any] = []
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs: Any) -> None:
        self.answers.append(text)


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


@pytest.mark.asyncio
async def test_oversized_text_is_rejected_without_mutating_saved_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = SimpleNamespace(
        position=1,
        title="Первый урок",
        content_kind=ContentKind.TEXT,
        content_ref="Короткий материал",
    )
    record = SimpleNamespace(
        program=SimpleNamespace(title="Программа"),
        lessons=[existing],
    )

    async def fake_load_draft(**_kwargs: Any) -> Any:
        return record

    monkeypatch.setattr(builder, "_load_draft", fake_load_draft)
    monkeypatch.setattr(
        builder,
        "add_program_lesson",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("oversized material must not be persisted")
        ),
    )

    state = FakeState(
        {
            "business_id": "1cd84a1e-626b-4eb9-bb9f-9dd7da118769",
            "program_id": "4f669f28-1880-4607-b67d-d86f19fca28b",
            "lesson_title": "Длинный урок",
        }
    )
    message = FakeMessage("x" * 2049)

    await builder.capture_lesson_content(message, state)

    assert record.lessons == [existing]
    assert state.data["lesson_title"] == "Длинный урок"
    assert state.clear_count == 0
    assert state.states == []
    assert "не более 2048 символов" in message.answers[-1]

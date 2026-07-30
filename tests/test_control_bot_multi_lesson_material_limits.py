from __future__ import annotations

import importlib
from typing import Any

import pytest

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
async def test_oversized_text_is_rejected_without_losing_session_lessons() -> None:
    existing = {
        "title": "Первый урок",
        "content_kind": "text",
        "content_ref": "Короткий материал",
    }
    state = FakeState(
        {
            "business_id": "1cd84a1e-626b-4eb9-bb9f-9dd7da118769",
            "program_title": "Программа",
            "lesson_title": "Длинный урок",
            "lessons": [existing],
        }
    )
    message = FakeMessage("x" * 2049)

    await builder.capture_lesson_content(message, state)

    assert state.data["lessons"] == [existing]
    assert state.data["lesson_title"] == "Длинный урок"
    assert state.clear_count == 0
    assert state.states == []
    assert "не более 2048 символов" in message.answers[-1]

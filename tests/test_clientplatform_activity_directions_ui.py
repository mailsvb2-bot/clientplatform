from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

import pytest

from clientplatform.domain.activity_directions import ActivityDirectionStatus
from handlers import clientplatform_activity_directions as directions_ui


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self, text: str | None = None, *, user_id: int = 101) -> None:
        self.text = text
        self.from_user = FakeUser(user_id)
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
    monkeypatch.setattr(directions_ui.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(
        directions_ui.control,
        "_callback_message",
        lambda callback: callback.message,
    )
    monkeypatch.setattr(directions_ui.control, "_user_id", lambda _message: 101)


@pytest.mark.asyncio
async def test_owner_can_create_and_open_generic_activity_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    direction_id = str(uuid4())
    actor = SimpleNamespace(assert_can_manage_business=lambda: None)

    async def fake_actor(_user_id: int, selected_business_id: str):
        assert selected_business_id == business_id
        return actor

    stored: list[Any] = []

    def list_directions(**_kwargs: Any) -> list[Any]:
        return list(stored)

    def create_direction(**kwargs: Any):
        direction = SimpleNamespace(
            id=direction_id,
            business_id=business_id,
            title=kwargs["title"],
            description=kwargs["description"],
            status=ActivityDirectionStatus.ACTIVE,
        )
        stored.append(direction)
        return direction

    monkeypatch.setattr(directions_ui.control, "_actor", fake_actor)
    monkeypatch.setattr(directions_ui, "list_activity_directions", list_directions)
    monkeypatch.setattr(directions_ui, "create_activity_direction", create_direction)
    monkeypatch.setattr(
        directions_ui,
        "list_activity_direction_bindings",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        directions_ui,
        "get_activity_direction",
        lambda **_kwargs: stored[0],
    )

    token = directions_ui.control._uuid_token(business_id)
    state = FakeState()
    start = FakeCallback(f"cp:diradd:{token}")
    await directions_ui.begin_activity_direction(start, state)
    assert state.states[-1] == directions_ui.ClientPlatformActivityDirectionState.title
    assert "Как называется направление деятельности?" in start.message.answers[-1][0]

    title = FakeMessage("Корпоративное направление")
    await directions_ui.capture_activity_direction_title(title, state)
    assert state.data["activity_direction_title"] == "Корпоративное направление"
    assert state.states[-1] == directions_ui.ClientPlatformActivityDirectionState.description

    description = FakeMessage("Работа с компаниями и командами.")
    await directions_ui.capture_activity_direction_description(description, state)
    assert stored[0].title == "Корпоративное направление"
    assert "общий бренд" in description.answers[-2][0]
    assert "Направления деятельности" in description.answers[-1][0]

    open_callback = FakeCallback(
        directions_ui._open_callback(business_id, direction_id)
    )
    await directions_ui.open_activity_direction(open_callback, FakeState())
    text = open_callback.message.answers[-1][0]
    assert "Корпоративное направление" in text
    assert "часть организации, а не отдельная организация" in text


@pytest.mark.asyncio
async def test_archiving_direction_preserves_links_and_can_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    direction_id = str(uuid4())
    actor = SimpleNamespace(assert_can_manage_business=lambda: None)
    current = SimpleNamespace(
        id=direction_id,
        business_id=business_id,
        title="Направление",
        description="Описание",
        status=ActivityDirectionStatus.ACTIVE,
    )

    async def fake_actor(_user_id: int, selected_business_id: str):
        assert selected_business_id == business_id
        return actor

    def archive(**_kwargs: Any):
        current.status = ActivityDirectionStatus.ARCHIVED
        return current

    def restore(**_kwargs: Any):
        current.status = ActivityDirectionStatus.ACTIVE
        return current

    monkeypatch.setattr(directions_ui.control, "_actor", fake_actor)
    monkeypatch.setattr(directions_ui, "archive_activity_direction", archive)
    monkeypatch.setattr(directions_ui, "restore_activity_direction", restore)

    business_token = directions_ui.control._uuid_token(business_id)
    direction_token = directions_ui.control._uuid_token(direction_id)

    archived = FakeCallback(f"cp:dirarc:{business_token}:{direction_token}")
    await directions_ui.archive_direction(archived, FakeState())
    assert "Связи и история сохранены" in archived.message.answers[-1][0]

    restored = FakeCallback(f"cp:dirrestore:{business_token}:{direction_token}")
    await directions_ui.restore_direction(restored, FakeState())
    assert "снова активно" in restored.message.answers[-1][0]


def test_direction_callbacks_fit_telegram_limit() -> None:
    business_id = str(uuid4())
    direction_id = str(uuid4())
    values = (
        directions_ui._open_callback(business_id, direction_id),
        f"cp:dirarc:{directions_ui.control._uuid_token(business_id)}:"
        f"{directions_ui.control._uuid_token(direction_id)}",
        f"cp:dirrestore:{directions_ui.control._uuid_token(business_id)}:"
        f"{directions_ui.control._uuid_token(direction_id)}",
    )
    assert all(len(value.encode("utf-8")) <= 64 for value in values)

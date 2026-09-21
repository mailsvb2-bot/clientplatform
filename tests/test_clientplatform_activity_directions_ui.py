from __future__ import annotations

import importlib.util
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from clientplatform.domain.activity_directions import ActivityDirectionStatus


_HAS_AIOGRAM = importlib.util.find_spec("aiogram") is not None
if _HAS_AIOGRAM:
    from handlers import clientplatform_activity_directions as directions_ui
else:  # dependency-light canon intentionally omits aiogram
    directions_ui = None


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


async def direct_to_thread(func, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@unittest.skipUnless(_HAS_AIOGRAM, "aiogram is not installed")
class ActivityDirectionsUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert directions_ui is not None
        self._patchers = [
            patch.object(directions_ui.asyncio, "to_thread", direct_to_thread),
            patch.object(
                directions_ui.control,
                "_callback_message",
                lambda callback: callback.message,
            ),
            patch.object(directions_ui.control, "_user_id", lambda _message: 101),
        ]
        for item in self._patchers:
            item.start()

    async def asyncTearDown(self) -> None:
        for item in reversed(self._patchers):
            item.stop()

    async def test_owner_can_create_and_open_generic_activity_direction(self) -> None:
        assert directions_ui is not None
        business_id = str(uuid4())
        direction_id = str(uuid4())
        actor = SimpleNamespace(assert_can_manage_business=lambda: None)

        async def fake_actor(_user_id: int, selected_business_id: str):
            self.assertEqual(selected_business_id, business_id)
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

        with (
            patch.object(directions_ui.control, "_actor", fake_actor),
            patch.object(directions_ui, "list_activity_directions", list_directions),
            patch.object(directions_ui, "create_activity_direction", create_direction),
            patch.object(
                directions_ui,
                "list_activity_direction_bindings",
                lambda **_kwargs: [],
            ),
            patch.object(
                directions_ui,
                "get_activity_direction",
                lambda **_kwargs: stored[0],
            ),
        ):
            token = directions_ui.control._uuid_token(business_id)
            state = FakeState()
            start = FakeCallback(f"cp:diradd:{token}")
            await directions_ui.begin_activity_direction(start, state)
            self.assertEqual(
                state.states[-1],
                directions_ui.ClientPlatformActivityDirectionState.title,
            )
            self.assertIn(
                "Как называется направление деятельности?",
                start.message.answers[-1][0],
            )

            title = FakeMessage("Корпоративное направление")
            await directions_ui.capture_activity_direction_title(title, state)
            self.assertEqual(
                state.data["activity_direction_title"],
                "Корпоративное направление",
            )

            description = FakeMessage("Работа с компаниями и командами.")
            await directions_ui.capture_activity_direction_description(
                description,
                state,
            )
            self.assertEqual(stored[0].title, "Корпоративное направление")
            self.assertIn("общий бренд", description.answers[-2][0])
            self.assertIn("Направления деятельности", description.answers[-1][0])

            open_callback = FakeCallback(
                directions_ui._open_callback(business_id, direction_id)
            )
            await directions_ui.open_activity_direction(
                open_callback,
                FakeState(),
            )
            text = open_callback.message.answers[-1][0]
            self.assertIn("Корпоративное направление", text)
            self.assertIn("часть организации, а не отдельная организация", text)
            self.assertNotIn("образователь", text.lower())

    async def test_active_direction_can_be_renamed_without_losing_identity(self) -> None:
        assert directions_ui is not None
        business_id = str(uuid4())
        direction_id = str(uuid4())
        actor = SimpleNamespace(assert_can_manage_business=lambda: None)
        current = SimpleNamespace(
            id=direction_id,
            business_id=business_id,
            title="Старое название",
            description="Старое описание",
            status=ActivityDirectionStatus.ACTIVE,
        )

        async def fake_actor(_user_id: int, selected_business_id: str):
            self.assertEqual(selected_business_id, business_id)
            return actor

        def update_direction(**kwargs: Any):
            self.assertEqual(kwargs["direction_id"], direction_id)
            current.title = kwargs["title"]
            current.description = kwargs["description"]
            return current

        with (
            patch.object(directions_ui.control, "_actor", fake_actor),
            patch.object(
                directions_ui,
                "get_activity_direction",
                lambda **_kwargs: current,
            ),
            patch.object(
                directions_ui,
                "update_activity_direction",
                update_direction,
            ),
            patch.object(
                directions_ui,
                "list_activity_directions",
                lambda **_kwargs: [current],
            ),
        ):
            business_token = directions_ui.control._uuid_token(business_id)
            direction_token = directions_ui.control._uuid_token(direction_id)
            state = FakeState()
            callback = FakeCallback(
                f"cp:diredit:{business_token}:{direction_token}"
            )
            await directions_ui.begin_edit_direction(callback, state)
            self.assertEqual(
                state.states[-1],
                directions_ui.ClientPlatformActivityDirectionState.edit_title,
            )

            await directions_ui.capture_edit_direction_title(
                FakeMessage("Новое название"),
                state,
            )
            description = FakeMessage("Новое описание направления")
            await directions_ui.capture_edit_direction_description(
                description,
                state,
            )
            self.assertEqual(current.id, direction_id)
            self.assertEqual(current.title, "Новое название")
            self.assertEqual(current.description, "Новое описание направления")
            self.assertIn("связи и история сохранены", description.answers[-2][0])

    async def test_archiving_direction_preserves_links_and_can_restore(self) -> None:
        assert directions_ui is not None
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
            self.assertEqual(selected_business_id, business_id)
            return actor

        def archive(**_kwargs: Any):
            current.status = ActivityDirectionStatus.ARCHIVED
            return current

        def restore(**_kwargs: Any):
            current.status = ActivityDirectionStatus.ACTIVE
            return current

        with (
            patch.object(directions_ui.control, "_actor", fake_actor),
            patch.object(directions_ui, "archive_activity_direction", archive),
            patch.object(directions_ui, "restore_activity_direction", restore),
        ):
            business_token = directions_ui.control._uuid_token(business_id)
            direction_token = directions_ui.control._uuid_token(direction_id)

            archived = FakeCallback(
                f"cp:dirarc:{business_token}:{direction_token}"
            )
            await directions_ui.archive_direction(archived, FakeState())
            self.assertIn(
                "Связи и история сохранены",
                archived.message.answers[-1][0],
            )

            restored = FakeCallback(
                f"cp:dirrestore:{business_token}:{direction_token}"
            )
            await directions_ui.restore_direction(restored, FakeState())
            self.assertIn("снова активно", restored.message.answers[-1][0])

    def test_direction_callbacks_fit_telegram_limit(self) -> None:
        assert directions_ui is not None
        business_id = str(uuid4())
        direction_id = str(uuid4())
        values = (
            directions_ui._open_callback(business_id, direction_id),
            f"cp:dirarc:{directions_ui.control._uuid_token(business_id)}:"
            f"{directions_ui.control._uuid_token(direction_id)}",
            f"cp:dirrestore:{directions_ui.control._uuid_token(business_id)}:"
            f"{directions_ui.control._uuid_token(direction_id)}",
            f"cp:diredit:{directions_ui.control._uuid_token(business_id)}:"
            f"{directions_ui.control._uuid_token(direction_id)}",
        )
        self.assertTrue(all(len(value.encode("utf-8")) <= 64 for value in values))


if __name__ == "__main__":
    unittest.main()

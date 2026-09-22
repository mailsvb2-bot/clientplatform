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

        archive_calls: list[str] = []

        def archive(**_kwargs: Any):
            archive_calls.append(direction_id)
            current.status = ActivityDirectionStatus.ARCHIVED
            return current

        def restore(**_kwargs: Any):
            current.status = ActivityDirectionStatus.ACTIVE
            return current

        with (
            patch.object(directions_ui.control, "_actor", fake_actor),
            patch.object(directions_ui, "get_activity_direction", return_value=current),
            patch.object(directions_ui, "archive_activity_direction", archive),
            patch.object(directions_ui, "restore_activity_direction", restore),
        ):
            business_token = directions_ui.control._uuid_token(business_id)
            direction_token = directions_ui.control._uuid_token(direction_id)

            prompt = FakeCallback(
                f"cp:dirarc:{business_token}:{direction_token}"
            )
            await directions_ui.archive_direction(prompt, FakeState())
            self.assertEqual(archive_calls, [])
            self.assertIn("Удалить направление", prompt.message.answers[-1][0])
            prompt_markup = prompt.message.answers[-1][1]["reply_markup"]
            self.assertEqual(
                prompt_markup.inline_keyboard[0][0].text,
                "🗑 Да, удалить направление",
            )
            self.assertTrue(
                str(prompt_markup.inline_keyboard[0][0].callback_data).startswith(
                    "cp:dirarcok:"
                )
            )

            archived = FakeCallback(
                f"cp:dirarcok:{business_token}:{direction_token}"
            )
            await directions_ui.archive_direction_confirm(archived, FakeState())
            self.assertEqual(archive_calls, [direction_id])
            self.assertIn(
                "Связи и история сохранены",
                archived.message.answers[-1][0],
            )

            restored = FakeCallback(
                f"cp:dirrestore:{business_token}:{direction_token}"
            )
            await directions_ui.restore_direction(restored, FakeState())
            self.assertIn("снова активно", restored.message.answers[-1][0])

    async def test_archived_navigation_binding_counts_and_validation_guards(self) -> None:
        assert directions_ui is not None
        business_id = str(uuid4())
        direction_id = str(uuid4())
        business_token = directions_ui.control._uuid_token(business_id)
        direction_token = directions_ui.control._uuid_token(direction_id)
        actor = SimpleNamespace(assert_can_manage_business=lambda: None)
        archived = SimpleNamespace(
            id=direction_id,
            business_id=business_id,
            title="Архивное направление",
            description="Историческое описание",
            status=ActivityDirectionStatus.ARCHIVED,
        )

        async def fake_actor(_user_id: int, selected_business_id: str):
            self.assertEqual(selected_business_id, business_id)
            return actor

        def list_directions(**kwargs: Any) -> list[Any]:
            return [archived]

        with (
            patch.object(directions_ui.control, "_actor", fake_actor),
            patch.object(directions_ui, "list_activity_directions", list_directions),
            patch.object(directions_ui, "get_activity_direction", lambda **_kwargs: archived),
            patch.object(
                directions_ui,
                "list_activity_direction_bindings",
                lambda **_kwargs: [
                    SimpleNamespace(subject_kind=SimpleNamespace(value="program")),
                    SimpleNamespace(subject_kind=SimpleNamespace(value="offering")),
                    SimpleNamespace(subject_kind=SimpleNamespace(value="event")),
                ],
            ),
        ):
            active_callback = FakeCallback(f"cp:dirs:{business_token}")
            active_state = FakeState({"old": True})
            await directions_ui.open_activity_directions(active_callback, active_state)
            self.assertEqual(active_state.clear_count, 1)

            archived_callback = FakeCallback(f"cp:dirarch:{business_token}")
            archived_state = FakeState({"old": True})
            await directions_ui.open_archived_activity_directions(
                archived_callback,
                archived_state,
            )
            labels = [
                button.text
                for row in archived_callback.message.answers[-1][1]["reply_markup"].inline_keyboard
                for button in row
            ]
            self.assertIn("Скрыть архив", labels)
            self.assertTrue(any(label.startswith("📦 ") for label in labels))

            opened = FakeCallback(f"cp:diropen:{business_token}:{direction_token}")
            await directions_ui.open_activity_direction(opened, FakeState())
            body = opened.message.answers[-1][0]
            self.assertIn("материалов: 1", body)
            self.assertIn("услуг и предложений: 1", body)
            self.assertIn("событий: 1", body)
            open_labels = [
                button.text
                for row in opened.message.answers[-1][1]["reply_markup"].inline_keyboard
                for button in row
            ]
            self.assertIn("♻️ Вернуть в работу", open_labels)

            edit = FakeCallback(f"cp:diredit:{business_token}:{direction_token}")
            edit_state = FakeState()
            await directions_ui.begin_edit_direction(edit, edit_state)
            self.assertEqual(edit_state.states, [])
            self.assertTrue(edit.answers[-1][1]["show_alert"])

        bad_title = FakeMessage("   ")
        await directions_ui.capture_activity_direction_title(bad_title, FakeState())
        self.assertIn("от 1 до 160", bad_title.answers[-1][0])

        missing_create = FakeMessage("Описание")
        missing_create_state = FakeState()
        await directions_ui.capture_activity_direction_description(
            missing_create,
            missing_create_state,
        )
        self.assertEqual(missing_create_state.clear_count, 1)
        self.assertIn("создание направления", missing_create.answers[-1][0].lower())

        bad_description = FakeMessage("   ")
        bad_description_state = FakeState(
            {
                "activity_direction_business_id": business_id,
                "activity_direction_title": "Название",
            }
        )
        await directions_ui.capture_activity_direction_description(
            bad_description,
            bad_description_state,
        )
        self.assertIn("от 1 до 2000", bad_description.answers[-1][0])

        duplicate_state = FakeState(
            {
                "activity_direction_business_id": business_id,
                "activity_direction_title": "Дубликат",
            }
        )
        duplicate = FakeMessage("Описание")
        with (
            patch.object(directions_ui.control, "_actor", fake_actor),
            patch.object(
                directions_ui,
                "create_activity_direction",
                side_effect=directions_ui.ActivityDirectionInvariantViolation("duplicate"),
            ),
        ):
            await directions_ui.capture_activity_direction_description(
                duplicate,
                duplicate_state,
            )
        self.assertIn("уже есть", duplicate.answers[-1][0])

    async def test_direction_edit_validation_guards_and_conflict(self) -> None:
        assert directions_ui is not None
        business_id = str(uuid4())
        direction_id = str(uuid4())
        actor = SimpleNamespace(assert_can_manage_business=lambda: None)

        async def fake_actor(_user_id: int, selected_business_id: str):
            self.assertEqual(selected_business_id, business_id)
            return actor

        missing_title = FakeMessage("Новое имя")
        missing_title_state = FakeState()
        await directions_ui.capture_edit_direction_title(
            missing_title,
            missing_title_state,
        )
        self.assertEqual(missing_title_state.clear_count, 1)
        self.assertIn("редактирование было закрыто", missing_title.answers[-1][0].lower())

        invalid_title = FakeMessage("   ")
        invalid_title_state = FakeState(
            {
                "activity_direction_business_id": business_id,
                "activity_direction_id": direction_id,
            }
        )
        await directions_ui.capture_edit_direction_title(
            invalid_title,
            invalid_title_state,
        )
        self.assertIn("от 1 до 160", invalid_title.answers[-1][0])

        missing_description = FakeMessage("Описание")
        missing_description_state = FakeState()
        await directions_ui.capture_edit_direction_description(
            missing_description,
            missing_description_state,
        )
        self.assertEqual(missing_description_state.clear_count, 1)

        invalid_description = FakeMessage("   ")
        invalid_description_state = FakeState(
            {
                "activity_direction_business_id": business_id,
                "activity_direction_id": direction_id,
                "activity_direction_edit_title": "Новое имя",
            }
        )
        await directions_ui.capture_edit_direction_description(
            invalid_description,
            invalid_description_state,
        )
        self.assertIn("от 1 до 2000", invalid_description.answers[-1][0])

        conflict = FakeMessage("Новое описание")
        conflict_state = FakeState(
            {
                "activity_direction_business_id": business_id,
                "activity_direction_id": direction_id,
                "activity_direction_edit_title": "Новое имя",
            }
        )
        with (
            patch.object(directions_ui.control, "_actor", fake_actor),
            patch.object(
                directions_ui,
                "update_activity_direction",
                side_effect=directions_ui.ActivityDirectionInvariantViolation("conflict"),
            ),
        ):
            await directions_ui.capture_edit_direction_description(
                conflict,
                conflict_state,
            )
        self.assertIn("Не удалось сохранить изменения", conflict.answers[-1][0])

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

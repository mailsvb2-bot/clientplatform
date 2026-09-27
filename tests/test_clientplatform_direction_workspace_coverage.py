from __future__ import annotations

import importlib.util
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

_HAS_AIOGRAM = importlib.util.find_spec("aiogram") is not None

if _HAS_AIOGRAM:
    from clientplatform.domain.activity_directions import ActivityDirectionStatus
    from handlers import clientplatform_activity_directions as directions_ui
    from handlers import clientplatform_control as control
    from handlers import clientplatform_direction_context as direction_context
    from handlers import clientplatform_program_builder as program_builder
else:
    ActivityDirectionStatus = None
    directions_ui = None
    control = None
    direction_context = None
    program_builder = None


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self) -> None:
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
        self.data: Any = dict(data or {})
        self.clear_count = 0

    async def get_data(self) -> Any:
        return self.data

    async def set_data(self, data: dict[str, Any]) -> None:
        self.data = dict(data)

    async def update_data(self, **kwargs: Any) -> None:
        if not isinstance(self.data, dict):
            self.data = {}
        self.data.update(kwargs)

    async def clear(self) -> None:
        self.clear_count += 1
        self.data = {}


async def direct_to_thread(func, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@unittest.skipUnless(_HAS_AIOGRAM, "aiogram is not installed")
class DirectionWorkspaceCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def test_direction_context_full_lifecycle_and_guards(self) -> None:
        assert direction_context is not None
        business_id = str(uuid4())
        direction_id = str(uuid4())
        state = FakeState()

        self.assertIsNone(
            await direction_context.read_direction_context(
                state, business_id=business_id
            )
        )

        state.data = []
        self.assertIsNone(
            await direction_context.read_direction_context(
                state, business_id=business_id
            )
        )

        state = FakeState()
        context = await direction_context.set_direction_context(
            state,
            business_id=business_id,
            direction_id=direction_id,
            title="  Корпоративное   направление  ",
        )
        self.assertEqual(context.title, "Корпоративное направление")
        self.assertEqual(
            await direction_context.read_direction_context(
                state, business_id=business_id
            ),
            context,
        )
        self.assertIsNone(
            await direction_context.read_direction_context(
                state, business_id=str(uuid4())
            )
        )

        missing = FakeState(
            {
                direction_context._DIRECTION_BUSINESS_KEY: business_id,
                direction_context._DIRECTION_ID_KEY: "",
                direction_context._DIRECTION_TITLE_KEY: "Название",
            }
        )
        self.assertIsNone(
            await direction_context.read_direction_context(
                missing, business_id=business_id
            )
        )
        missing.data[direction_context._DIRECTION_ID_KEY] = direction_id
        missing.data[direction_context._DIRECTION_TITLE_KEY] = "   "
        self.assertIsNone(
            await direction_context.read_direction_context(
                missing, business_id=business_id
            )
        )

        with self.assertRaises(ValueError):
            await direction_context.set_direction_context(
                state,
                business_id=business_id,
                direction_id=direction_id,
                title="   ",
            )

        restored = await direction_context.clear_flow_preserving_direction(
            state, business_id=business_id
        )
        self.assertEqual(restored, context)
        self.assertEqual(state.clear_count, 1)
        self.assertEqual(
            await direction_context.read_direction_context(
                state, business_id=business_id
            ),
            context,
        )

        await direction_context.clear_direction_context(state)
        self.assertIsNone(
            await direction_context.read_direction_context(
                state, business_id=business_id
            )
        )
        self.assertEqual(direction_context.direction_heading(None), "")
        self.assertIn(
            "Корпоративное направление",
            direction_context.direction_heading(context),
        )

        empty = FakeState()
        self.assertIsNone(
            await direction_context.clear_flow_preserving_direction(
                empty, business_id=business_id
            )
        )

    async def test_direction_goal_routes_keep_scope_for_every_action(self) -> None:
        assert directions_ui is not None
        assert direction_context is not None
        business_id = str(uuid4())
        direction_id = str(uuid4())
        business_token = directions_ui.control._uuid_token(business_id)
        direction_token = directions_ui.control._uuid_token(direction_id)
        actor = SimpleNamespace()
        direction = SimpleNamespace(
            id=direction_id,
            title="Консультации",
            status=ActivityDirectionStatus.ACTIVE,
        )

        async def fake_actor(_user_id: int, selected_business_id: str):
            self.assertEqual(selected_business_id, business_id)
            return actor

        routes = {
            "a": ("clientplatform_one_click_experience", "open_ad_tools", "cpo:ads:"),
            "c": ("clientplatform_one_click_experience", "open_client_tools", "cpo:clients:"),
            "e": ("clientplatform_events", "open_event_hub", "cpev:home:"),
            "w": ("clientplatform_one_click_experience", "open_work_tools", "cpo:work:"),
            "p": ("clientplatform_program_builder", "open_programs", "cp:cap:"),
        }

        for goal, (module_suffix, method_name, expected_prefix) in routes.items():
            with self.subTest(goal=goal):
                called = AsyncMock()
                module = SimpleNamespace(**{method_name: called})
                state = FakeState()
                callback = FakeCallback(
                    f"cp:dg:{goal}:{business_token}:{direction_token}"
                )
                real_import = directions_ui.importlib.import_module

                def fake_import(name: str, package: str | None = None):
                    if name.endswith(module_suffix):
                        return module
                    return real_import(name, package)

                with (
                    patch.object(directions_ui.asyncio, "to_thread", direct_to_thread),
                    patch.object(directions_ui.control, "_actor", fake_actor),
                    patch.object(
                        directions_ui,
                        "get_activity_direction",
                        return_value=direction,
                    ),
                    patch.object(
                        directions_ui.importlib,
                        "import_module",
                        side_effect=fake_import,
                    ),
                ):
                    await directions_ui.open_activity_direction_goal(callback, state)

                called.assert_awaited_once()
                routed_callback = called.await_args.args[0]
                self.assertTrue(str(routed_callback.data).startswith(expected_prefix))
                scoped = await direction_context.read_direction_context(
                    state, business_id=business_id
                )
                self.assertIsNotNone(scoped)
                self.assertEqual(scoped.direction_id, direction_id)

        result_call = AsyncMock()
        state = FakeState()
        callback = FakeCallback(f"cp:dg:r:{business_token}:{direction_token}")
        with (
            patch.object(directions_ui.asyncio, "to_thread", direct_to_thread),
            patch.object(directions_ui.control, "_actor", fake_actor),
            patch.object(directions_ui, "get_activity_direction", return_value=direction),
            patch.object(directions_ui.control, "show_results", result_call),
        ):
            await directions_ui.open_activity_direction_goal(callback, state)
        result_call.assert_awaited_once()
        self.assertTrue(
            str(result_call.await_args.args[0].data).startswith("cp:results:")
        )

        stale = FakeCallback(f"cp:dg:x:{business_token}:{direction_token}")
        with (
            patch.object(directions_ui.asyncio, "to_thread", direct_to_thread),
            patch.object(directions_ui.control, "_actor", fake_actor),
            patch.object(directions_ui, "get_activity_direction", return_value=direction),
        ):
            await directions_ui.open_activity_direction_goal(stale, FakeState())
        self.assertTrue(stale.answers[-1][1]["show_alert"])

        archived = SimpleNamespace(
            id=direction_id,
            title="Архив",
            status=ActivityDirectionStatus.ARCHIVED,
        )
        inactive = FakeCallback(f"cp:dg:a:{business_token}:{direction_token}")
        with (
            patch.object(directions_ui.asyncio, "to_thread", direct_to_thread),
            patch.object(directions_ui.control, "_actor", fake_actor),
            patch.object(directions_ui, "get_activity_direction", return_value=archived),
        ):
            await directions_ui.open_activity_direction_goal(inactive, FakeState())
        self.assertTrue(inactive.answers[-1][1]["show_alert"])

    async def test_archived_direction_settings_render_restore_path(self) -> None:
        assert directions_ui is not None
        business_id = str(uuid4())
        direction_id = str(uuid4())
        business_token = directions_ui.control._uuid_token(business_id)
        direction_token = directions_ui.control._uuid_token(direction_id)
        actor = SimpleNamespace(assert_can_manage_business=lambda: None)
        direction = SimpleNamespace(
            id=direction_id,
            title="Архив",
            description="Описание",
            status=ActivityDirectionStatus.ARCHIVED,
        )

        async def fake_actor(_user_id: int, _business_id: str):
            return actor

        callback = FakeCallback(
            f"cp:dirset:{business_token}:{direction_token}"
        )
        with (
            patch.object(directions_ui.asyncio, "to_thread", direct_to_thread),
            patch.object(directions_ui.control, "_actor", fake_actor),
            patch.object(directions_ui, "get_activity_direction", return_value=direction),
            patch.object(
                directions_ui.control,
                "_callback_message",
                lambda item: item.message,
            ),
        ):
            await directions_ui.open_activity_direction_settings(
                callback, FakeState()
            )

        labels = [
            button.text
            for row in callback.message.answers[-1][1][
                "reply_markup"
            ].inline_keyboard
            for button in row
        ]
        self.assertEqual(labels, ["♻️ Вернуть в работу", "📦 К архиву"])

    async def test_scoped_results_filter_programs_and_handle_empty_scope(self) -> None:
        assert control is not None
        assert direction_context is not None
        business_id = str(uuid4())
        direction_id = str(uuid4())
        business_token = control._uuid_token(business_id)
        direction_token = control._uuid_token(direction_id)
        actor = SimpleNamespace()
        direction = SimpleNamespace(id=direction_id, title="Обучение")
        included_program = str(uuid4())
        excluded_program = str(uuid4())
        progress = [
            SimpleNamespace(
                program_id=included_program,
                customer_id="customer-1",
                customer_display_name="Анна",
                program_title="Курс A",
                completed_lessons=2,
                total_lessons=4,
                percent_complete=50,
            ),
            SimpleNamespace(
                program_id=excluded_program,
                customer_id="customer-2",
                customer_display_name="Борис",
                program_title="Курс B",
                completed_lessons=1,
                total_lessons=5,
                percent_complete=20,
            ),
        ]

        async def fake_actor(_user_id: int, selected_business_id: str):
            self.assertEqual(selected_business_id, business_id)
            return actor

        callback = FakeCallback(
            f"cp:results:{business_token}:{direction_token}"
        )
        state = FakeState()
        with (
            patch.object(control.asyncio, "to_thread", direct_to_thread),
            patch.object(control, "_actor", fake_actor),
            patch.object(control, "_callback_actor_user_id", return_value=101),
            patch.object(control, "_callback_message", lambda item: item.message),
            patch.object(control, "get_activity_direction", return_value=direction),
            patch.object(
                control,
                "list_business_program_progress",
                return_value=progress,
            ),
            patch.object(
                control,
                "list_activity_direction_bindings",
                return_value=[SimpleNamespace(subject_id=included_program)],
            ),
        ):
            await control.show_results(callback, state)

        text = callback.message.answers[-1][0]
        self.assertIn("🧭 Направление: Обучение", text)
        self.assertIn("Курс A", text)
        self.assertNotIn("Курс B", text)
        self.assertIn("Связано программ: 1", text)
        self.assertIn("Клиентов с прогрессом: 1", text)

        empty_callback = FakeCallback(
            f"cp:results:{business_token}:{direction_token}"
        )
        with (
            patch.object(control.asyncio, "to_thread", direct_to_thread),
            patch.object(control, "_actor", fake_actor),
            patch.object(control, "_callback_actor_user_id", return_value=101),
            patch.object(control, "_callback_message", lambda item: item.message),
            patch.object(control, "get_activity_direction", return_value=direction),
            patch.object(
                control,
                "list_business_program_progress",
                return_value=[],
            ),
            patch.object(
                control,
                "list_activity_direction_bindings",
                return_value=[],
            ),
        ):
            await control.show_results(empty_callback, None)
        self.assertIn(
            "По программам этого направления пока нет прогресса.",
            empty_callback.message.answers[-1][0],
        )

        stale = FakeCallback("cp:results:too:many:parts:here")
        await control.show_results(stale, FakeState())
        self.assertTrue(stale.answers[-1][1]["show_alert"])


    async def test_small_direction_workspace_guard_branches(self) -> None:
        assert directions_ui is not None
        business_id = str(uuid4())
        direction_id = str(uuid4())
        business_token = directions_ui.control._uuid_token(business_id)
        direction_token = directions_ui.control._uuid_token(direction_id)

        class LegacyCallback:
            def __init__(self) -> None:
                self.data = "old"

        legacy = LegacyCallback()
        self.assertIs(
            directions_ui._routed_callback(legacy, "new"),
            legacy,
        )
        self.assertEqual(legacy.data, "new")

        actor = SimpleNamespace()
        archived = SimpleNamespace(
            id=direction_id,
            title="Архив",
            status=ActivityDirectionStatus.ARCHIVED,
        )

        async def fake_actor(_user_id: int, _business_id: str):
            return actor

        callback = FakeCallback(
            f"cp:dirarc:{business_token}:{direction_token}"
        )
        with (
            patch.object(directions_ui.asyncio, "to_thread", direct_to_thread),
            patch.object(directions_ui.control, "_actor", fake_actor),
            patch.object(directions_ui, "get_activity_direction", return_value=archived),
        ):
            await directions_ui.archive_direction(callback, FakeState())
        self.assertTrue(callback.answers[-1][1]["show_alert"])

    async def test_program_direction_selection_guards_and_success_paths(self) -> None:
        assert program_builder is not None
        business_id = str(uuid4())
        direction_id = str(uuid4())
        business_token = program_builder.control._uuid_token(business_id)
        direction_token = program_builder.control._uuid_token(direction_id)
        actor = SimpleNamespace()

        async def fake_actor(_user_id: int, selected_business_id: str):
            self.assertEqual(selected_business_id, business_id)
            return actor

        missing = FakeCallback(
            f"cp:progdir:{business_token}:{direction_token}"
        )
        missing_state = FakeState()
        with (
            patch.object(program_builder.asyncio, "to_thread", direct_to_thread),
            patch.object(program_builder.control, "_actor", fake_actor),
            patch.object(program_builder, "list_activity_directions", return_value=[]),
        ):
            await program_builder.choose_program_direction(missing, missing_state)
        self.assertTrue(missing.answers[-1][1]["show_alert"])

        direction = SimpleNamespace(id=direction_id)
        chosen = FakeCallback(
            f"cp:progdir:{business_token}:{direction_token}"
        )
        chosen_state = FakeState()
        with (
            patch.object(program_builder.asyncio, "to_thread", direct_to_thread),
            patch.object(program_builder.control, "_actor", fake_actor),
            patch.object(
                program_builder,
                "list_activity_directions",
                return_value=[direction],
            ),
            patch.object(
                program_builder.control,
                "_callback_message",
                lambda item: item.message,
            ),
        ):
            await program_builder.choose_program_direction(chosen, chosen_state)
        self.assertEqual(chosen_state.data["business_id"], business_id)
        self.assertEqual(chosen_state.data["direction_id"], direction_id)
        self.assertIn("Напишите название программы", chosen.message.answers[-1][0])

        unscoped = FakeCallback(f"cp:progdirnone:{business_token}")
        unscoped_state = FakeState()
        with (
            patch.object(program_builder.control, "_actor", fake_actor),
            patch.object(
                program_builder.control,
                "_callback_message",
                lambda item: item.message,
            ),
        ):
            await program_builder.choose_program_without_direction(
                unscoped, unscoped_state
            )
        self.assertEqual(unscoped_state.data["business_id"], business_id)
        self.assertIsNone(unscoped_state.data["direction_id"])

    async def test_program_draft_keyboard_hides_add_at_limit(self) -> None:
        assert program_builder is not None
        business_id = str(uuid4())
        program_id = str(uuid4())
        program = SimpleNamespace(
            business_id=business_id,
            id=program_id,
        )
        below_limit = SimpleNamespace(
            program=program,
            lessons=[object()] * (program_builder._MAX_LESSONS_PER_PROGRAM - 1),
        )
        at_limit = SimpleNamespace(
            program=program,
            lessons=[object()] * program_builder._MAX_LESSONS_PER_PROGRAM,
        )
        below = program_builder._draft_keyboard(below_limit)
        full = program_builder._draft_keyboard(at_limit)
        below_labels = [button.text for row in below.inline_keyboard for button in row]
        full_labels = [button.text for row in full.inline_keyboard for button in row]
        self.assertIn("Добавить ещё урок", below_labels)
        self.assertNotIn("Добавить ещё урок", full_labels)


if __name__ == "__main__":
    unittest.main()

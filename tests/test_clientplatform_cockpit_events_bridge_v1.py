from __future__ import annotations

import importlib.util
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

_RUNTIME_AVAILABLE = importlib.util.find_spec("aiogram") is not None

if _RUNTIME_AVAILABLE:
    from handlers import clientplatform_cockpit_dispatch as cockpit_dispatch

_BUSINESS = "11111111-1111-4111-8111-111111111111"


@unittest.skipUnless(_RUNTIME_AVAILABLE, "aiogram runtime dependency is not installed")
class CockpitEventsBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_section_renders_current_funnel_and_creation_button(self) -> None:
        snapshot = SimpleNamespace(
            can_manage=True,
            can_enable_commercial_followups=True,
            commercial_followups_enabled=False,
            commercial_followups_platform_available=True,
            limitations=(),
            items=(SimpleNamespace(
                title="Вебинар по гипнозу", local_start="15.09.2026 19:00",
                registered=42, join_clicked=31, attendance_confirmed=24,
                offer_clicked=11, paid=5,
                revenue=(SimpleNamespace(display="25 000 RUB"),),
            ),),
        )
        target = SimpleNamespace(answer=AsyncMock())
        captured_rows: list[list[tuple[str, str]]] = []

        def keyboard(rows: list[list[tuple[str, str]]]):
            captured_rows.extend(rows)
            return rows

        with (
            patch.object(cockpit_dispatch, "resolve_cockpit_events", return_value=snapshot),
            patch.object(cockpit_dispatch.one_click.control, "_keyboard", side_effect=keyboard),
        ):
            await cockpit_dispatch.send_cockpit_section(
                target, user_id=101, business_id=_BUSINESS, section="events"
            )

        target.answer.assert_awaited_once()
        text = target.answer.await_args.args[0]
        self.assertIn("Вебинары", text)
        self.assertIn("регистрации 42", text)
        self.assertIn("пришли 24", text)
        self.assertIn("оплаты 5", text)
        self.assertIn("25 000 RUB", text)
        self.assertIn("Автосообщения вебинара: ⚪️ выключены", text)
        self.assertNotIn("После включения — кому писать:", text)
        self.assertNotIn("После включения — каналы:", text)
        flattened = [button for row in captured_rows for button in row]
        token = cockpit_dispatch.one_click.control._uuid_token(_BUSINESS)
        self.assertIn(("⚙️ Автосообщения", f"cpev:settings:{token}"), flattened)
        self.assertTrue(any(
            label == "🎥 Создать вебинар" and callback.startswith("cpev:new:")
            for label, callback in flattened
        ))

    async def test_events_section_shows_stored_on_state_during_platform_shutdown(self) -> None:
        snapshot = SimpleNamespace(
            can_manage=True,
            can_enable_commercial_followups=False,
            commercial_followups_enabled=True,
            commercial_followups_effective=False,
            commercial_followups_platform_available=False,
            limitations=(),
            items=(),
        )
        target = SimpleNamespace(answer=AsyncMock())
        captured_rows: list[list[tuple[str, str]]] = []
        with (
            patch.object(cockpit_dispatch, "resolve_cockpit_events", return_value=snapshot),
            patch.object(
                cockpit_dispatch.one_click.control,
                "_keyboard",
                side_effect=lambda rows: captured_rows.extend(rows) or rows,
            ),
        ):
            await cockpit_dispatch.send_cockpit_section(
                target, user_id=101, business_id=_BUSINESS, section="events"
            )
        text = target.answer.await_args.args[0]
        self.assertIn("Автосообщения вебинара: 🟡 включены, временно приостановлены", text)
        flattened = [button for row in captured_rows for button in row]
        self.assertIn(("⚙️ Автосообщения", f"cpev:settings:{cockpit_dispatch.one_click.control._uuid_token(_BUSINESS)}"), flattened)
        self.assertFalse(any(callback.startswith("cpev:followups:") for _label, callback in flattened))

    async def test_events_section_shows_active_autosend_and_disable_button(self) -> None:
        snapshot = SimpleNamespace(
            can_manage=True,
            can_enable_commercial_followups=False,
            commercial_followups_enabled=True,
            commercial_followups_effective=True,
            commercial_followups_platform_available=True,
            limitations=(),
            items=(),
        )
        target = SimpleNamespace(answer=AsyncMock())
        captured_rows: list[list[tuple[str, str]]] = []
        with (
            patch.object(cockpit_dispatch, "resolve_cockpit_events", return_value=snapshot),
            patch.object(
                cockpit_dispatch.one_click.control, "_keyboard",
                side_effect=lambda rows: captured_rows.extend(rows) or rows,
            ),
        ):
            await cockpit_dispatch.send_cockpit_section(
                target, user_id=101, business_id=_BUSINESS, section="events"
            )
        text = target.answer.await_args.args[0]
        self.assertIn("Автосообщения вебинара: 🟢 включены", text)
        self.assertNotIn("Кому писать:", text)
        flattened = [button for row in captured_rows for button in row]
        self.assertIn(("⚙️ Автосообщения", f"cpev:settings:{cockpit_dispatch.one_click.control._uuid_token(_BUSINESS)}"), flattened)

    async def test_events_section_shows_platform_pause_when_preference_is_off(self) -> None:
        snapshot = SimpleNamespace(
            can_manage=True,
            can_enable_commercial_followups=False,
            commercial_followups_enabled=False,
            commercial_followups_effective=False,
            commercial_followups_platform_available=False,
            limitations=("Ограничение тестового бизнеса",),
            items=(),
        )
        target = SimpleNamespace(answer=AsyncMock())
        captured_rows: list[list[tuple[str, str]]] = []
        with (
            patch.object(cockpit_dispatch, "resolve_cockpit_events", return_value=snapshot),
            patch.object(
                cockpit_dispatch.one_click.control, "_keyboard",
                side_effect=lambda rows: captured_rows.extend(rows) or rows,
            ),
        ):
            await cockpit_dispatch.send_cockpit_section(
                target, user_id=101, business_id=_BUSINESS, section="events"
            )
        text = target.answer.await_args.args[0]
        self.assertIn("Автосообщения вебинара: ⚪️ выключены", text)
        self.assertNotIn("Ограничение тестового бизнеса", text)

    async def test_events_section_hides_creation_for_read_only_snapshot(self) -> None:
        snapshot = SimpleNamespace(
            can_manage=False,
            can_enable_commercial_followups=False,
            commercial_followups_enabled=False,
            commercial_followups_effective=False,
            commercial_followups_platform_available=True,
            limitations=(),
            items=(),
        )
        target = SimpleNamespace(answer=AsyncMock())
        captured_rows: list[list[tuple[str, str]]] = []
        with (
            patch.object(cockpit_dispatch, "resolve_cockpit_events", return_value=snapshot),
            patch.object(
                cockpit_dispatch.one_click.control, "_keyboard",
                side_effect=lambda rows: captured_rows.extend(rows) or rows,
            ),
        ):
            await cockpit_dispatch.send_cockpit_section(
                target, user_id=101, business_id=_BUSINESS, section="events"
            )
        flattened = [button for row in captured_rows for button in row]
        self.assertFalse(any(callback.startswith("cpev:new:") for _label, callback in flattened))


    async def test_events_section_rejects_unknown_semantic_action(self) -> None:
        snapshot = SimpleNamespace(items=())
        target = SimpleNamespace(answer=AsyncMock())
        unknown = SimpleNamespace(kind="unknown", label="Неизвестно", key=None, enabled=None)
        with (
            patch.object(cockpit_dispatch, "resolve_cockpit_events", return_value=snapshot),
            patch.object(cockpit_dispatch, "event_hub_actions", return_value=(unknown,)),
        ):
            with self.assertRaisesRegex(ValueError, "unsupported event hub action"):
                await cockpit_dispatch.send_cockpit_section(
                    target, user_id=101, business_id=_BUSINESS, section="events"
                )
        target.answer.assert_not_awaited()

    async def test_cockpit_action_route_rejects_unknown_route(self) -> None:
        target = SimpleNamespace(answer=AsyncMock())
        route = SimpleNamespace(
            section=None, kind="unknown", lead_id=None, business_id=_BUSINESS
        )
        with self.assertRaisesRegex(ValueError, "unsupported cockpit action route"):
            await cockpit_dispatch.send_cockpit_action_route(
                target, user_id=101, route=route
            )
        target.answer.assert_not_awaited()


    async def test_events_section_routes_join_and_announce_actions(self) -> None:
        snapshot = SimpleNamespace(items=())
        target = SimpleNamespace(answer=AsyncMock())
        event_id = "33333333-3333-4333-8333-333333333333"
        event_token = "MzMzMzMzQzODMzMzMzMzMzMzMz"
        business_token = "ERERERERQRGBEREREREREQ"
        actions = (
            SimpleNamespace(kind="join", label="🔗 Добавить ссылку", key=event_id),
            SimpleNamespace(kind="announce", label="✨ Сделать анонс", key=event_id),
        )

        def token(value: str) -> str:
            return event_token if value == event_id else business_token

        with (
            patch.object(cockpit_dispatch, "resolve_cockpit_events", return_value=snapshot),
            patch.object(cockpit_dispatch, "event_hub_actions", return_value=actions),
            patch.object(cockpit_dispatch, "event_hub_text", return_value="Вебинары"),
            patch.object(cockpit_dispatch.one_click.control, "_uuid_token", side_effect=token),
            patch.object(cockpit_dispatch.one_click.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            await cockpit_dispatch.send_cockpit_section(
                target, user_id=101, business_id=_BUSINESS, section="events"
            )

        rows = target.answer.await_args.kwargs["reply_markup"]
        flattened = [button for row in rows for button in row]
        self.assertIn(("🔗 Добавить ссылку", f"cpev:join:{event_token}:{business_token}"), flattened)
        self.assertIn(("✨ Сделать анонс", f"cpev:announce:{event_token}:{business_token}"), flattened)


if __name__ == "__main__":
    unittest.main()

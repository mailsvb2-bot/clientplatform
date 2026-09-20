from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from clientplatform.application.events import (
    EventUnavailable,
    cancel_public_registration_by_token_in_transaction,
)
from clientplatform.infrastructure.event_repository import EventNotFound

try:
    from clientplatform.runtime import public_events as public_events_runtime
except ModuleNotFoundError as exc:
    if exc.name != "aiohttp":
        raise
    public_events_runtime = None


class _Cursor:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _CancellationConnection:
    def __init__(self, *, registration_update_count: int = 1) -> None:
        self.registration_update_count = registration_update_count
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> _Cursor:
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if "UPDATE clientplatform_event_registrations" in normalized:
            return _Cursor(self.registration_update_count)
        return _Cursor(1)


class ParticipantCancellationApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registration = SimpleNamespace(
            id="33333333-3333-4333-8333-333333333333",
            business_id="11111111-1111-4111-8111-111111111111",
            event_id="22222222-2222-4222-8222-222222222222",
        )
        self.repository = Mock()
        self.repository.get_registration_by_token.return_value = self.registration
        self.now = datetime(2026, 10, 1, 9, 15, 30, tzinfo=timezone.utc)

    def test_cancellation_stops_pending_messages_and_revokes_commercial_consent(self) -> None:
        conn = _CancellationConnection()
        revoke = Mock(return_value=2)

        with (
            patch(
                "clientplatform.application.events.EventRepository",
                return_value=self.repository,
            ),
            patch(
                "clientplatform.application.event_commercial_consent."
                "revoke_event_commercial_consent_by_registration_token_in_transaction",
                revoke,
            ),
        ):
            result = cancel_public_registration_by_token_in_transaction(
                conn,
                token="registration-capability",
                now=self.now,
            )

        self.assertIs(result, self.registration)
        self.repository.get_registration_by_token.assert_called_once_with(
            token="registration-capability"
        )
        self.assertEqual(len(conn.calls), 2)
        self.assertIn(
            "SET status='cancelled'",
            conn.calls[0][0],
        )
        self.assertIn(
            "last_error='event_registration_cancelled'",
            conn.calls[1][0],
        )
        revoke.assert_called_once_with(
            conn,
            token="registration-capability",
            now=self.now,
        )

    def test_stale_cancellation_fails_closed_before_side_effects(self) -> None:
        conn = _CancellationConnection(registration_update_count=0)
        revoke = Mock()

        with (
            patch(
                "clientplatform.application.events.EventRepository",
                return_value=self.repository,
            ),
            patch(
                "clientplatform.application.event_commercial_consent."
                "revoke_event_commercial_consent_by_registration_token_in_transaction",
                revoke,
            ),
            self.assertRaisesRegex(EventUnavailable, "no longer active"),
        ):
            cancel_public_registration_by_token_in_transaction(
                conn,
                token="registration-capability",
                now=self.now,
            )

        self.assertEqual(len(conn.calls), 1)
        revoke.assert_not_called()


@unittest.skipIf(public_events_runtime is None, "aiohttp is not installed")
class ParticipantSelfServiceSurfaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.registration = SimpleNamespace(
            id="33333333-3333-4333-8333-333333333333",
            business_id="11111111-1111-4111-8111-111111111111",
            event_id="22222222-2222-4222-8222-222222222222",
            name="Иван <script>",
        )
        self.event = SimpleNamespace(
            title="Вебинар <безопасный>",
            timezone_name="Europe/Moscow",
        )
        self.sessions = (
            SimpleNamespace(
                position=1,
                starts_at=datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                position=2,
                starts_at=datetime(2026, 10, 11, 15, 0, tzinfo=timezone.utc),
            ),
        )

    def test_manage_body_is_personal_escaped_and_has_cancel_action(self) -> None:
        body = public_events_runtime._manage_body(
            "capability-token",
            self.registration,
            self.event,
            self.sessions,
            ("telegram", "vk"),
        )

        self.assertIn("Вебинар &lt;безопасный&gt;", body)
        self.assertIn("Иван &lt;script&gt;", body)
        self.assertNotIn("<script>", body)
        self.assertIn("10.10.2026 18:00", body)
        self.assertIn("/e/join/capability-token/1", body)
        self.assertIn("Telegram, ВКонтакте", body)
        self.assertIn(
            "/e/manage/capability-token/cancel",
            body,
        )

    async def test_manage_handler_renders_existing_registration(self) -> None:
        request = SimpleNamespace(match_info={"token": "capability-token"})
        with patch.object(
            public_events_runtime.asyncio,
            "to_thread",
            new=AsyncMock(
                return_value=(
                    self.registration,
                    self.event,
                    self.sessions,
                    ("max",),
                )
            ),
        ):
            response = await public_events_runtime.public_event_manage(request)

        self.assertEqual(response.status, 200)
        self.assertIn("Моя регистрация", response.text)
        self.assertIn("MAX", response.text)

    async def test_cancel_handler_uses_same_registration_capability(self) -> None:
        request = SimpleNamespace(match_info={"token": "capability-token"})
        cancel = Mock(return_value=self.registration)

        with (
            patch.object(
                public_events_runtime,
                "get_db",
                return_value=nullcontext(object()),
            ),
            patch.object(
                public_events_runtime,
                "cancel_public_registration_by_token_in_transaction",
                cancel,
            ),
        ):
            response = await public_events_runtime.public_event_cancel_registration(
                request
            )

        self.assertEqual(response.status, 200)
        self.assertIn("Регистрация отменена", response.text)
        cancel.assert_called_once_with(
            unittest.mock.ANY,
            token="capability-token",
        )

    async def test_cancel_handler_hides_stale_or_unknown_capability(self) -> None:
        request = SimpleNamespace(match_info={"token": "expired-token"})

        with (
            patch.object(
                public_events_runtime,
                "get_db",
                return_value=nullcontext(object()),
            ),
            patch.object(
                public_events_runtime,
                "cancel_public_registration_by_token_in_transaction",
                side_effect=EventNotFound("registration token is invalid"),
            ),
        ):
            response = await public_events_runtime.public_event_cancel_registration(
                request
            )

        self.assertEqual(response.status, 404)
        self.assertIn("Регистрация уже недоступна", response.text)


if __name__ == "__main__":
    unittest.main()

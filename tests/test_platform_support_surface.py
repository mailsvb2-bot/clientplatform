from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import clientplatform_entry as entry
from services import platform_support_access as support


class PlatformSupportSurfaceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _message(text: str, *, message_id: int = 77):
        return SimpleNamespace(
            text=text,
            message_id=message_id,
            chat=SimpleNamespace(id=500),
            from_user=SimpleNamespace(id=9001),
            answer=AsyncMock(),
        )

    async def test_open_uses_message_identity_as_idempotency_key(self) -> None:
        business_id = "11111111-1111-1111-1111-111111111111"
        session = SimpleNamespace(
            id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            business_id=business_id,
            expires_at="2026-09-02T12:30:00+00:00",
        )
        message = self._message(
            f"/supportsession open {business_id} INC-263 delivery incident"
        )
        with (
            patch.object(entry.control, "_user_id", return_value=9001),
            patch.object(support, "issue_support_session", return_value=session) as issue,
        ):
            await entry.clientplatform_platform_support_session_command(message)

        issue.assert_called_once_with(
            9001,
            business_id=business_id,
            ticket_ref="INC-263",
            reason="delivery incident",
            idempotency_key="telegram:500:77",
        )
        text = message.answer.await_args.args[0]
        self.assertIn(session.id, text)
        self.assertIn("read-only", text)

    async def test_read_is_bound_to_session_and_exact_business(self) -> None:
        session_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        business_id = "11111111-1111-1111-1111-111111111111"
        snapshot = SimpleNamespace(
            session_id=session_id,
            business_id=business_id,
            business_name="North Star",
            business_status="active",
            session_expires_at="2026-09-02T12:30:00+00:00",
        )
        message = self._message(f"/supportsession read {session_id} {business_id}")
        with (
            patch.object(entry.control, "_user_id", return_value=9001),
            patch.object(support, "read_support_business", return_value=snapshot) as read,
        ):
            await entry.clientplatform_platform_support_session_command(message)

        read.assert_called_once_with(
            9001,
            session_id=session_id,
            business_id=business_id,
        )
        text = message.answer.await_args.args[0]
        self.assertIn("North Star", text)
        self.assertIn("read-only", text)

    async def test_revoke_calls_capability_owner(self) -> None:
        session_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        business_id = "11111111-1111-1111-1111-111111111111"
        session = SimpleNamespace(
            id=session_id,
            business_id=business_id,
            revoked_at="2026-09-02T12:10:00+00:00",
        )
        message = self._message(f"/supportsession revoke {session_id} {business_id}")
        with (
            patch.object(entry.control, "_user_id", return_value=9001),
            patch.object(support, "revoke_support_session", return_value=session) as revoke,
        ):
            await entry.clientplatform_platform_support_session_command(message)

        revoke.assert_called_once_with(
            9001,
            session_id=session_id,
            business_id=business_id,
        )
        self.assertIn("отозвана", message.answer.await_args.args[0])

    async def test_permission_denial_does_not_expose_support_details(self) -> None:
        business_id = "11111111-1111-1111-1111-111111111111"
        message = self._message(
            f"/supportsession open {business_id} INC-263 delivery incident"
        )
        with (
            patch.object(entry.control, "_user_id", return_value=17),
            patch.object(
                support,
                "issue_support_session",
                side_effect=support.PlatformSupportPermissionDenied(
                    "platform support access required"
                ),
            ),
        ):
            await entry.clientplatform_platform_support_session_command(message)

        message.answer.assert_awaited_once_with(
            "Доступ к support session недоступен."
        )

    async def test_support_command_is_not_registered_in_public_menu(self) -> None:
        bot = SimpleNamespace(set_my_commands=AsyncMock(return_value=True))
        self.assertTrue(await entry.register_clientplatform_bot_commands(bot))
        commands = bot.set_my_commands.await_args.args[0]
        self.assertNotIn("supportsession", {item.command for item in commands})


if __name__ == "__main__":
    unittest.main()

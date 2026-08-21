from __future__ import annotations

import unittest
from uuid import uuid4

from clientplatform.application import native_member_interactions as member_ui
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import PlatformRole, TenantContext


class NativeMemberSetupUxTests(unittest.TestCase):
    def _actor(self, role: PlatformRole, *, user_id: int = 1001) -> TenantContext:
        return TenantContext(
            business_id=str(uuid4()),
            membership_id=str(uuid4()),
            user_id=user_id,
            role=role,
        )

    def test_owner_connect_vk_emits_only_non_secret_setup_command(self) -> None:
        actor = self._actor(PlatformRole.OWNER)
        session_id = str(uuid4())
        calls: list[tuple[TenantContext, ConnectionPlatform, str]] = []

        def issuer(
            tenant: TenantContext,
            platform: ConnectionPlatform,
            setup_key: str,
        ) -> str:
            calls.append((tenant, platform, setup_key))
            return f"cpm:setup:{session_id}"

        message = member_ui._render(
            actor,
            member_ui.ParsedMemberInteraction("connect-vk"),
            linked=False,
            setup_issuer=issuer,
            setup_key="route:r:event:e:member:1001:action:connect-vk",
        )

        self.assertEqual(1, len(calls))
        self.assertEqual(ConnectionPlatform.VK, calls[0][1])
        self.assertEqual(f"cpm:setup:{session_id}", message.rows[0][0].command)
        durable = message.to_json()
        self.assertNotIn("/clientplatform/connect/", durable)
        self.assertNotIn("https://client", durable)

    def test_administrator_can_issue_max_setup(self) -> None:
        actor = self._actor(PlatformRole.ADMINISTRATOR)
        calls: list[ConnectionPlatform] = []

        def issuer(
            _tenant: TenantContext,
            platform: ConnectionPlatform,
            _setup_key: str,
        ) -> str:
            calls.append(platform)
            return f"cpm:setup:{uuid4()}"

        message = member_ui._render(
            actor,
            member_ui.ParsedMemberInteraction("connect-max"),
            linked=False,
            setup_issuer=issuer,
            setup_key="route:r:event:e:member:1001:action:connect-max",
        )

        self.assertEqual([ConnectionPlatform.MAX], calls)
        self.assertTrue(message.rows[0][0].command.startswith("cpm:setup:"))

    def test_support_direct_connect_command_is_denied_without_calling_issuer(self) -> None:
        actor = self._actor(PlatformRole.SUPPORT)
        called = False

        def issuer(
            _tenant: TenantContext,
            _platform: ConnectionPlatform,
            _setup_key: str,
        ) -> str:
            nonlocal called
            called = True
            return f"cpm:setup:{uuid4()}"

        parsed = member_ui.parse_native_member_interaction("cpm:connect-vk")
        self.assertEqual("connect-vk", parsed.action)
        message = member_ui._render(
            actor,
            parsed,
            linked=False,
            setup_issuer=issuer,
            setup_key="forged-support-command",
        )

        self.assertFalse(called)
        self.assertIn("недоступен", message.text.casefold())
        self.assertEqual("cpm:menu", message.rows[0][0].command)

    def test_setup_issuance_failure_is_sanitized(self) -> None:
        actor = self._actor(PlatformRole.OWNER)

        def issuer(
            _tenant: TenantContext,
            _platform: ConnectionPlatform,
            _setup_key: str,
        ) -> str:
            raise RuntimeError("secret://env/SHOULD_NOT_LEAK")

        message = member_ui._render(
            actor,
            member_ui.ParsedMemberInteraction("connect-max"),
            linked=False,
            setup_issuer=issuer,
            setup_key="route:r:event:e:member:1001:action:connect-max",
        )

        self.assertIn("Не удалось подготовить", message.text)
        self.assertNotIn("SHOULD_NOT_LEAK", message.to_json())


if __name__ == "__main__":
    unittest.main()

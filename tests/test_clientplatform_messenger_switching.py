from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application import native_member_interactions as member_ui
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.native_messenger_setup_repository import (
    NativeMessengerSetupRepository,
)
from clientplatform.runtime.messenger_switch_links import StaffMessengerSwitchLinkService
from services.db.schema import clientplatform_messenger_channels, clientplatform_tenancy


def _actor() -> TenantContext:
    return TenantContext(
        business_id=str(uuid4()),
        user_id=101,
        membership_id=str(uuid4()),
        role=PlatformRole.OWNER,
    )


class NativeMessengerMenuTests(unittest.TestCase):
    def test_vk_menu_offers_missing_telegram_and_switch_to_active_max(self) -> None:
        actor = _actor()
        connections = [
            SimpleNamespace(platform=ConnectionPlatform.VK, status=SimpleNamespace(value="active")),
            SimpleNamespace(platform=ConnectionPlatform.MAX, status=SimpleNamespace(value="active")),
        ]
        with (
            patch.object(member_ui, "list_connections", return_value=connections),
            patch(
                "clientplatform.application.capability_parity.telegram_runtime_enabled",
                return_value=True,
            ),
            patch(
                "clientplatform.application.capability_parity._omnichannel_runtime_enabled",
                return_value=True,
            ),
            patch(
                "clientplatform.application.capability_parity.build_setup_status",
                return_value=SimpleNamespace(telegram_ok=True, vk_ok=True, max_ok=True),
            ),
            patch.object(
                member_ui,
                "available_staff_messenger_switches",
                return_value=(ConnectionPlatform.VK, ConnectionPlatform.MAX),
            ),
            patch.object(
                member_ui,
                "build_staff_switch_command",
                side_effect=lambda _actor, platform: f"cpm:switch:{platform.value}:101",
            ),
        ):
            message = member_ui._messengers_message(
                actor,
                setup_available=True,
                current_platform=ConnectionPlatform.VK,
            )
        labels = [button.label for row in message.rows for button in row]
        commands = [button.command for row in message.rows for button in row]
        self.assertIn("✈️ Подключить Telegram", labels)
        self.assertIn("🟣 Перейти в MAX", labels)
        self.assertNotIn("🔵 Перейти во ВКонтакте", labels)
        self.assertIn("cpm:connect-telegram", commands)
        self.assertIn("cpm:switch:max:101", commands)


class StaffMessengerSwitchLinkTests(unittest.TestCase):
    def test_link_service_materializes_platform_specific_one_time_urls(self) -> None:
        service = StaffMessengerSwitchLinkService()
        destination = SimpleNamespace(
            user_id=101,
            business_id=str(uuid4()),
            platform=ConnectionPlatform.TELEGRAM,
            public_target="owner_business_bot",
        )
        with (
            patch(
                "clientplatform.runtime.messenger_switch_links.resolve_staff_switch_destination",
                return_value=destination,
            ),
            patch(
                "clientplatform.runtime.messenger_switch_links.issue_bridge_token",
                return_value="bridge-token",
            ) as issue,
        ):
            url = service.resolve_command_url(
                command="cpm:switch:telegram:101",
                business_id=destination.business_id,
            )
        self.assertEqual(
            "https://t.me/owner_business_bot?start=bridge_bridge-token",
            url,
        )
        issue.assert_called_once_with(101, target_platform="telegram")


class TelegramSetupCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_messenger_channels.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=777, name="Business")
        self.actor = tenancy.resolve_context(
            user_id=777,
            business_id=access.business.id,
        )
        self.repo = NativeMessengerSetupRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_owner_can_issue_telegram_setup_capability(self) -> None:
        issued = self.repo.issue(
            actor=self.actor,
            platform=ConnectionPlatform.TELEGRAM,
            ttl_seconds=600,
        )
        self.assertEqual(ConnectionPlatform.TELEGRAM, issued.platform)

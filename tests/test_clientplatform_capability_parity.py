from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.application.capability_parity import (
    CapabilityAvailability,
    get_business_capability_projection,
    project_messenger_capabilities,
)
from clientplatform.domain.connections import ConnectionPlatform, ConnectionStatus


class CapabilityParityTests(unittest.TestCase):
    def _project(
        self,
        connections=(),
        *,
        enabled=None,
        ready=None,
        setup=True,
    ):
        enabled = enabled or {
            ConnectionPlatform.TELEGRAM: True,
            ConnectionPlatform.VK: False,
            ConnectionPlatform.MAX: False,
        }
        ready = ready or {
            ConnectionPlatform.TELEGRAM: True,
            ConnectionPlatform.VK: True,
            ConnectionPlatform.MAX: True,
        }
        return {
            item.platform: item
            for item in project_messenger_capabilities(
                connections,
                setup_available=setup,
                runtime_enabled=enabled,
                runtime_ready=ready,
            )
        }

    def test_disabled_vk_and_max_are_not_connectable(self) -> None:
        projected = self._project()
        self.assertEqual(
            projected[ConnectionPlatform.TELEGRAM].availability,
            CapabilityAvailability.CONNECTABLE,
        )
        self.assertTrue(projected[ConnectionPlatform.TELEGRAM].can_connect)
        for platform in (ConnectionPlatform.VK, ConnectionPlatform.MAX):
            self.assertEqual(
                projected[platform].availability,
                CapabilityAvailability.UNAVAILABLE,
            )
            self.assertFalse(projected[platform].can_connect)

    def test_enum_like_connection_values_are_normalized(self) -> None:
        connection = SimpleNamespace(
            platform=SimpleNamespace(value="max"),
            status=SimpleNamespace(value="active"),
        )
        enabled = {
            ConnectionPlatform.TELEGRAM: True,
            ConnectionPlatform.VK: False,
            ConnectionPlatform.MAX: True,
        }
        projected = self._project((connection,), enabled=enabled)
        self.assertEqual(
            projected[ConnectionPlatform.MAX].availability,
            CapabilityAvailability.ACTIVE,
        )

    def test_active_connection_with_disabled_runtime_is_explicitly_unavailable(self) -> None:
        connection = SimpleNamespace(
            platform=ConnectionPlatform.VK,
            status=ConnectionStatus.ACTIVE,
        )
        projected = self._project((connection,))
        self.assertEqual(
            projected[ConnectionPlatform.VK].availability,
            CapabilityAvailability.CONNECTED_UNAVAILABLE,
        )
        self.assertFalse(projected[ConnectionPlatform.VK].can_connect)

    def test_enabled_ready_channel_without_connection_is_connectable(self) -> None:
        enabled = {
            ConnectionPlatform.TELEGRAM: True,
            ConnectionPlatform.VK: True,
            ConnectionPlatform.MAX: True,
        }
        ready = {platform: True for platform in enabled}
        projected = self._project(enabled=enabled, ready=ready)
        self.assertEqual(
            projected[ConnectionPlatform.VK].availability,
            CapabilityAvailability.CONNECTABLE,
        )
        self.assertTrue(projected[ConnectionPlatform.VK].can_connect)

    def test_runtime_not_ready_fails_closed(self) -> None:
        enabled = {
            ConnectionPlatform.TELEGRAM: True,
            ConnectionPlatform.VK: True,
            ConnectionPlatform.MAX: False,
        }
        ready = {
            ConnectionPlatform.TELEGRAM: True,
            ConnectionPlatform.VK: False,
            ConnectionPlatform.MAX: True,
        }
        projected = self._project(enabled=enabled, ready=ready)
        self.assertEqual(
            projected[ConnectionPlatform.VK].availability,
            CapabilityAvailability.UNAVAILABLE,
        )
        self.assertFalse(projected[ConnectionPlatform.VK].can_connect)

    def test_attention_connection_remains_visible_as_attention(self) -> None:
        connection = SimpleNamespace(
            platform=ConnectionPlatform.MAX,
            status=ConnectionStatus.ATTENTION,
        )
        projected = self._project((connection,))
        self.assertEqual(
            projected[ConnectionPlatform.MAX].availability,
            CapabilityAvailability.ATTENTION,
        )

    def test_setup_surface_off_hides_connect_action(self) -> None:
        projected = self._project(setup=False)
        telegram = projected[ConnectionPlatform.TELEGRAM]
        self.assertEqual(telegram.availability, CapabilityAvailability.UNAVAILABLE)
        self.assertFalse(telegram.can_connect)

    def test_business_projection_uses_read_only_connection_status_projection(self) -> None:
        actor = SimpleNamespace(user_id=7, business_id="business-1")
        setup_status = SimpleNamespace(telegram_ok=True, vk_ok=True, max_ok=True)
        with (
            patch("clientplatform.application.capability_parity.business_connection_statuses", return_value=[(ConnectionPlatform.VK, ConnectionStatus.ACTIVE)]) as statuses,
            patch("clientplatform.application.capability_parity._omnichannel_setup_available", return_value=True),
            patch("clientplatform.application.capability_parity.telegram_runtime_enabled", return_value=True),
            patch("clientplatform.application.capability_parity.vk_webhook_enabled", return_value=False),
            patch("clientplatform.application.capability_parity.max_webhook_enabled", return_value=False),
            patch("clientplatform.application.capability_parity.build_setup_status", return_value=setup_status),
        ):
            projection = get_business_capability_projection(
                actor=actor,
                include_advertising=False,
            )

        statuses.assert_called_once_with(actor=actor)
        self.assertEqual(
            projection.messenger(ConnectionPlatform.VK).availability,
            CapabilityAvailability.CONNECTED_UNAVAILABLE,
        )

    def test_generic_omnichannel_setup_does_not_enable_disabled_vk_max_runtime(self) -> None:
        setup_status = SimpleNamespace(telegram_ok=True, vk_ok=True, max_ok=True)
        with (
            patch("clientplatform.application.capability_parity._omnichannel_setup_available", return_value=True),
            patch("clientplatform.application.capability_parity.telegram_runtime_enabled", return_value=True),
            patch("clientplatform.application.capability_parity.vk_webhook_enabled", return_value=False),
            patch("clientplatform.application.capability_parity.max_webhook_enabled", return_value=False),
            patch("clientplatform.application.capability_parity.build_setup_status", return_value=setup_status),
        ):
            projected = {
                item.platform: item
                for item in project_messenger_capabilities(())
            }

        self.assertTrue(projected[ConnectionPlatform.TELEGRAM].can_connect)
        self.assertFalse(projected[ConnectionPlatform.VK].can_connect)
        self.assertFalse(projected[ConnectionPlatform.MAX].can_connect)
        self.assertEqual(
            projected[ConnectionPlatform.VK].availability,
            CapabilityAvailability.UNAVAILABLE,
        )
        self.assertEqual(
            projected[ConnectionPlatform.MAX].availability,
            CapabilityAvailability.UNAVAILABLE,
        )


if __name__ == "__main__":
    unittest.main()

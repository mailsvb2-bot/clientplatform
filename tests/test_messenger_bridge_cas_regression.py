from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from services.messenger import bridge


class _LostUpdateCursor:
    rowcount = 0


class _LostUpdateConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, statement: str, params: tuple[object, ...]):
        self.calls.append((statement, params))
        return _LostUpdateCursor()


class MessengerBridgeCompareAndSetTests(unittest.TestCase):
    def test_concurrent_token_consumer_losing_compare_and_set_returns_none(self) -> None:
        conn = _LostUpdateConnection()
        unresolved = bridge.BridgeResolution(
            canonical_user_id=101,
            token="one-time-token",
            consumed=False,
            target_platform="max",
        )

        with patch.object(
            bridge,
            "_resolve_bridge_token_in_conn",
            return_value=unresolved,
        ):
            result = bridge._consume_bridge_token_in_conn(
                conn,
                raw="one-time-token",
                norm="max",
                external_user_id="  external-42  ",
            )

        self.assertIsNone(result)
        self.assertEqual(len(conn.calls), 1)
        statement, params = conn.calls[0]
        self.assertIn("used_at IS NULL", statement)
        self.assertEqual(params[1], "max")
        self.assertEqual(params[2], "external-42")
        self.assertEqual(params[3], 101)
        self.assertEqual(params[4], "one-time-token")
        self.assertEqual(params[5], bridge.PURPOSE_SWITCH)


class MessengerBridgeInConnectionTests(unittest.TestCase):
    def test_resolve_in_conn_rejects_empty_and_strips_token(self) -> None:
        conn = object()
        self.assertIsNone(bridge.resolve_bridge_token_in_conn(conn, "   "))
        expected = bridge.BridgeResolution(101, "token", False, "vk")
        with patch.object(bridge, "_resolve_bridge_token_in_conn", return_value=expected) as resolver:
            result = bridge.resolve_bridge_token_in_conn(conn, "  token  ")
        self.assertIs(result, expected)
        resolver.assert_called_once_with(conn, "token")

    def test_resolve_in_conn_rejects_missing_and_expired_rows(self) -> None:
        missing = MagicMock()
        missing.execute.return_value.fetchone.return_value = None
        self.assertIsNone(bridge._resolve_bridge_token_in_conn(missing, "missing"))

        expired = MagicMock()
        expired.execute.return_value.fetchone.return_value = {
            "token": "expired",
            "user_id": 101,
            "used_at": None,
            "created_at": None,
            "account_id": 101,
            "target_platform": "vk",
            "expires_at": "2025-01-01T00:00:00",
        }
        with patch.object(bridge, "utc_now", return_value=datetime(2026, 1, 1)):
            self.assertIsNone(bridge._resolve_bridge_token_in_conn(expired, "expired"))

    def test_consume_and_link_in_conn_validates_then_links_once(self) -> None:
        conn = object()
        self.assertIsNone(
            bridge.consume_bridge_token_and_link_in_conn(
                conn, "   ", platform="vk", external_user_id="vk-1"
            )
        )
        self.assertIsNone(
            bridge.consume_bridge_token_and_link_in_conn(
                conn, "token", platform="unknown", external_user_id="x"
            )
        )
        expected = bridge.BridgeResolution(101, "token", True, "vk")
        with (
            patch.object(bridge, "_consume_bridge_token_in_conn", return_value=expected) as consume,
            patch.object(bridge, "_link_channel_to_account_in_conn") as link,
        ):
            result = bridge.consume_bridge_token_and_link_in_conn(
                conn,
                "  token  ",
                platform="VK",
                external_user_id="vk-42",
                username="owner",
                display_name="Owner",
            )
        self.assertIs(result, expected)
        consume.assert_called_once_with(
            conn, raw="token", norm="vk", external_user_id="vk-42"
        )
        link.assert_called_once_with(
            conn,
            101,
            "vk",
            "vk-42",
            username="owner",
            display_name="Owner",
            verified=True,
            link_source="bridge",
        )

    def test_consume_and_link_in_conn_does_not_link_when_consume_loses(self) -> None:
        conn = object()
        with (
            patch.object(bridge, "_consume_bridge_token_in_conn", return_value=None),
            patch.object(bridge, "_link_channel_to_account_in_conn") as link,
        ):
            result = bridge.consume_bridge_token_and_link_in_conn(
                conn, "token", platform="max", external_user_id="max-42"
            )
        self.assertIsNone(result)
        link.assert_not_called()

    def test_public_consume_helpers_fail_closed_on_invalid_inputs(self) -> None:
        self.assertIsNone(bridge.consume_bridge_token("", platform="vk", external_user_id="x"))
        self.assertIsNone(
            bridge.consume_bridge_token("token", platform="unknown", external_user_id="x")
        )
        self.assertIsNone(
            bridge.consume_bridge_token_and_link("", platform="vk", external_user_id="x")
        )
        self.assertEqual(bridge._row_value({}, "missing", "fallback"), "fallback")


if __name__ == "__main__":
    unittest.main()

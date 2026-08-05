from __future__ import annotations

import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from clientplatform.application import max_dispatch_pacing
from clientplatform.application.dispatch_worker import (
    _claims_releasable_after_cancel,
    _effective_max_attempts,
)
from clientplatform.domain.connections import ConnectionPlatform
from runtime.messenger_max_sender import MaxProviderRateLimitError


ROOT = Path(__file__).resolve().parents[1]


def _item(
    *,
    platform: ConnectionPlatform = ConnectionPlatform.MAX,
    connection_id: str = "connection-a",
    external_subject: str = "user-1",
):
    return SimpleNamespace(
        external_subject=external_subject,
        dispatch=SimpleNamespace(
            platform=platform,
            connection_id=connection_id,
        ),
    )


class MaxDispatchPacingTests(unittest.TestCase):
    def setUp(self) -> None:
        max_dispatch_pacing._next_connection_write_at.clear()
        max_dispatch_pacing._next_dialog_write_at.clear()

    def test_max_dialog_is_kept_below_two_messages_per_second(self) -> None:
        item = _item()

        self.assertEqual(
            max_dispatch_pacing._reserve_max_provider_slot(item, now=100.0),
            0.0,
        )
        delay = max_dispatch_pacing._reserve_max_provider_slot(item, now=100.0)

        self.assertAlmostEqual(delay, 0.55, places=9)

    def test_max_connection_has_global_request_safety_margin(self) -> None:
        first = _item(external_subject="user-1")
        second = _item(external_subject="user-2")

        self.assertEqual(
            max_dispatch_pacing._reserve_max_provider_slot(first, now=200.0),
            0.0,
        )
        delay = max_dispatch_pacing._reserve_max_provider_slot(second, now=200.0)

        self.assertAlmostEqual(delay, 0.04, places=9)

    def test_max_connections_do_not_throttle_each_other(self) -> None:
        first = _item(connection_id="connection-a", external_subject="same-user")
        second = _item(connection_id="connection-b", external_subject="same-user")

        self.assertEqual(
            max_dispatch_pacing._reserve_max_provider_slot(first, now=300.0),
            0.0,
        )
        self.assertEqual(
            max_dispatch_pacing._reserve_max_provider_slot(second, now=300.0),
            0.0,
        )

    def test_vk_dispatch_is_not_affected_by_max_pacing(self) -> None:
        vk_item = _item(platform=ConnectionPlatform.VK)

        self.assertEqual(
            max_dispatch_pacing._reserve_max_provider_slot(vk_item, now=400.0),
            0.0,
        )
        self.assertEqual(max_dispatch_pacing._next_connection_write_at, {})
        self.assertEqual(max_dispatch_pacing._next_dialog_write_at, {})

    def test_max_media_prepare_pace_marker_and_final_write_order_is_locked(self) -> None:
        source = (
            ROOT / "clientplatform" / "application" / "dispatch_worker.py"
        ).read_text(encoding="utf-8")
        body = source.split("async def run_dispatch_batch", 1)[1]

        prepare = body.index("prepared = await adapter.prepare(send_item, credential)")
        pace = body.index("await pace_max_provider_boundary(send_item)")
        non_replay = body.index("_mark_non_replay_boundary")
        final_write = body.index(
            "provider_message_id = await two_phase_adapter.send_prepared("
        )

        self.assertLess(prepare, pace)
        self.assertLess(pace, non_replay)
        self.assertLess(non_replay, final_write)
        self.assertIn("if waited_for_max_slot or prepared is not None:", body)
        self.assertGreaterEqual(
            body.count("_provider_claim_can_cross_provider_boundary"),
            2,
        )
        self.assertIn("await _release_prepared_dispatch", body)
        self.assertIn("prepared,\n                    credential,", body)

    def test_cancellation_requeues_current_only_before_non_replay_marker(self) -> None:
        claimed = ["current", "next", "later"]

        self.assertEqual(
            claimed,
            _claims_releasable_after_cancel(
                claimed,
                current_index=0,
                non_replay_boundary_crossed=False,
            ),
        )
        self.assertEqual(
            ["next", "later"],
            _claims_releasable_after_cancel(
                claimed,
                current_index=0,
                non_replay_boundary_crossed=True,
            ),
        )

    def test_max_429_remains_replay_safe_after_non_replay_marker(self) -> None:
        rate_limited = MaxProviderRateLimitError(
            "MAX rate limited",
            code="max.http_429",
        )

        self.assertTrue(rate_limited.provider_write_definitely_rejected)
        self.assertEqual(
            _effective_max_attempts(
                rate_limited,
                8,
                non_replay_boundary_crossed=True,
            ),
            8,
        )

    def test_unknown_post_boundary_failure_is_not_automatically_replayed(self) -> None:
        self.assertEqual(
            _effective_max_attempts(
                RuntimeError("ambiguous provider outcome"),
                8,
                non_replay_boundary_crossed=True,
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()

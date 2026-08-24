from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from clientplatform.application import max_dispatch_pacing
from clientplatform.domain.connections import ConnectionPlatform


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


def _reset() -> None:
    max_dispatch_pacing._next_connection_write_at.clear()
    max_dispatch_pacing._next_dialog_write_at.clear()


def test_max_dialog_is_kept_below_two_messages_per_second() -> None:
    _reset()
    item = _item()

    assert max_dispatch_pacing._reserve_max_provider_slot(item, now=100.0) == 0.0
    delay = max_dispatch_pacing._reserve_max_provider_slot(item, now=100.0)

    assert delay == pytest.approx(0.55)


def test_max_connection_has_global_request_safety_margin() -> None:
    _reset()
    first = _item(external_subject="user-1")
    second = _item(external_subject="user-2")

    assert max_dispatch_pacing._reserve_max_provider_slot(first, now=200.0) == 0.0
    delay = max_dispatch_pacing._reserve_max_provider_slot(second, now=200.0)

    assert delay == pytest.approx(0.04)


def test_max_connections_do_not_throttle_each_other() -> None:
    _reset()
    first = _item(connection_id="connection-a", external_subject="same-user")
    second = _item(connection_id="connection-b", external_subject="same-user")

    assert max_dispatch_pacing._reserve_max_provider_slot(first, now=300.0) == 0.0
    assert max_dispatch_pacing._reserve_max_provider_slot(second, now=300.0) == 0.0


def test_vk_dispatch_is_not_affected_by_max_pacing() -> None:
    _reset()
    vk_item = _item(platform=ConnectionPlatform.VK)

    assert max_dispatch_pacing._reserve_max_provider_slot(vk_item, now=400.0) == 0.0
    assert max_dispatch_pacing._next_connection_write_at == {}
    assert max_dispatch_pacing._next_dialog_write_at == {}


def test_max_pacing_stays_before_non_replay_boundary() -> None:
    source = (
        ROOT / "clientplatform" / "application" / "dispatch_worker.py"
    ).read_text(encoding="utf-8")
    body = source.split("async def run_dispatch_batch", 1)[1]

    pace = body.index("await pace_max_provider_boundary(send_item)")
    non_replay = body.index("_mark_non_replay_boundary")
    provider_send = body.index("provider_message_id = await adapter.send")

    assert pace < non_replay < provider_send
    assert "if waited_for_max_slot:" in body
    assert body.count("_provider_claim_can_cross_provider_boundary") >= 2

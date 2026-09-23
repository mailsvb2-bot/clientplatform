from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from core.time_utils import utc_now, utc_now_iso
from services.bg import tm
from services.db import db
from runtime.messenger_max_sender import MaxProviderRejectedError
from services.messenger import delivery_outbox, delivery_pool


def _insert_outbox(
    *,
    platform: str,
    user_id: int,
    event_key: str,
    status: str = "pending",
    created_at: str | None = None,
    updated_at: str | None = None,
    sent_at: str | None = None,
) -> int:
    now = utc_now_iso()
    created = created_at or now
    updated = updated_at or created
    available = created
    with db() as conn:
        conn.execute(
            """
            INSERT INTO messenger_delivery_outbox(
                platform,external_user_id,canonical_user_id,event_key,action,replies_json,
                status,attempts,available_at,locked_at,lock_token,last_error,
                created_at,updated_at,sent_at
            ) VALUES(?,?,?,?,?,'[]',?,0,?,NULL,NULL,'',?,?,?)
            """.strip(),
            (
                platform,
                str(user_id),
                int(user_id),
                event_key,
                "test",
                status,
                available,
                created,
                updated,
                sent_at,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM messenger_delivery_outbox WHERE platform=? AND event_key=?",
            (platform, event_key),
        ).fetchone()
    return int(row["id"])


def test_stream_head_preserves_order_inside_one_user() -> None:
    user_id = 884001
    first_id = _insert_outbox(platform="vk", user_id=user_id, event_key="pool-order-first-884001")
    second_id = _insert_outbox(platform="vk", user_id=user_id, event_key="pool-order-second-884001")

    first = delivery_pool.claim_stream_head(platform="vk", lock_ttl_sec=900)
    assert first is not None
    assert first.id == first_id
    assert delivery_pool.claim_stream_head(platform="vk", lock_ttl_sec=900) is None

    delivery_outbox.mark_delivery_sent(first)
    second = delivery_pool.claim_stream_head(platform="vk", lock_ttl_sec=900)
    assert second is not None
    assert second.id == second_id
    delivery_outbox.mark_delivery_sent(second)


def test_different_user_streams_can_be_leased_concurrently() -> None:
    first_id = _insert_outbox(platform="max", user_id=884011, event_key="pool-user-a-884011")
    second_id = _insert_outbox(platform="max", user_id=884012, event_key="pool-user-b-884012")

    first = delivery_pool.claim_stream_head(platform="max", lock_ttl_sec=900)
    second = delivery_pool.claim_stream_head(platform="max", lock_ttl_sec=900)

    assert first is not None
    assert second is not None
    assert {first.id, second.id} == {first_id, second_id}
    assert first.canonical_user_id != second.canonical_user_id
    delivery_outbox.mark_delivery_sent(first)
    delivery_outbox.mark_delivery_sent(second)


def test_platform_worker_limits_are_independent(monkeypatch) -> None:
    monkeypatch.setenv("MESSENGER_OUTBOX_VK_WORKERS", "3")
    monkeypatch.setenv("MESSENGER_OUTBOX_MAX_WORKERS", "5")
    assert delivery_pool.configured_worker_counts() == {"vk": 3, "max": 5}


@pytest.mark.asyncio
async def test_slow_delivery_does_not_block_unrelated_user(monkeypatch) -> None:
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    completed: list[str] = []

    async def fake_deliver(item: delivery_outbox.ClaimedDelivery) -> None:
        if item.event_key == "pool-slow":
            slow_started.set()
            await release_slow.wait()
        completed.append(item.event_key)

    monkeypatch.setattr(delivery_outbox, "_deliver_one", fake_deliver)
    slow = delivery_outbox.ClaimedDelivery(
        id=1,
        platform="vk",
        external_user_id="884021",
        canonical_user_id=884021,
        event_key="pool-slow",
        action="test",
        replies_json="[]",
        attempts=0,
        lock_token="slow-token",
    )
    fast = delivery_outbox.ClaimedDelivery(
        id=2,
        platform="vk",
        external_user_id="884022",
        canonical_user_id=884022,
        event_key="pool-fast",
        action="test",
        replies_json="[]",
        attempts=0,
        lock_token="fast-token",
    )

    slow_task = tm().create(
        delivery_pool._process_item(slow),  # noqa: SLF001
        name="test_delivery_pool_slow",
    )
    await asyncio.wait_for(slow_started.wait(), timeout=2)
    fast_task = tm().create(
        delivery_pool._process_item(fast),  # noqa: SLF001
        name="test_delivery_pool_fast",
    )
    await asyncio.wait_for(fast_task, timeout=2)

    assert completed == ["pool-fast"]
    release_slow.set()
    await asyncio.wait_for(slow_task, timeout=2)
    assert completed == ["pool-fast", "pool-slow"]


def test_retention_deletes_only_expired_terminal_evidence() -> None:
    old = (utc_now().replace(microsecond=0) - timedelta(days=400)).isoformat()
    fresh = utc_now_iso()
    old_sent = _insert_outbox(
        platform="vk",
        user_id=884031,
        event_key="pool-old-sent-884031",
        status="sent",
        created_at=old,
        updated_at=old,
        sent_at=old,
    )
    fresh_sent = _insert_outbox(
        platform="vk",
        user_id=884032,
        event_key="pool-fresh-sent-884032",
        status="sent",
        created_at=fresh,
        updated_at=fresh,
        sent_at=fresh,
    )
    old_dead = _insert_outbox(
        platform="max",
        user_id=884033,
        event_key="pool-old-dead-884033",
        status="dead",
        created_at=old,
        updated_at=old,
    )
    fresh_dead = _insert_outbox(
        platform="max",
        user_id=884034,
        event_key="pool-fresh-dead-884034",
        status="dead",
        created_at=fresh,
        updated_at=fresh,
    )

    with db() as conn:
        conn.execute(
            """
            INSERT INTO messenger_webhook_events(
                platform,event_key,received_at,payload_hash,status,attempts,
                updated_at,completed_at,last_error
            ) VALUES('vk','pool-old-webhook-884031',?,'hash','completed',1,?,?,'')
            """.strip(),
            (old, old, old),
        )
        conn.execute(
            """
            INSERT INTO messenger_webhook_events(
                platform,event_key,received_at,payload_hash,status,attempts,
                updated_at,completed_at,last_error
            ) VALUES('vk','pool-fresh-webhook-884032',?,'hash','completed',1,?,?,'')
            """.strip(),
            (fresh, fresh, fresh),
        )
        conn.commit()

    result = delivery_pool.cleanup_delivery_history(
        sent_retention_days=30,
        dead_retention_days=180,
        webhook_retention_days=30,
        batch_size=100,
    )
    assert result.sent_deleted >= 1
    assert result.dead_deleted >= 1
    assert result.webhook_deleted >= 1

    with db() as conn:
        ids = {
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM messenger_delivery_outbox WHERE id IN (?,?,?,?)",
                (old_sent, fresh_sent, old_dead, fresh_dead),
            ).fetchall()
        }
        webhooks = {
            str(row["event_key"])
            for row in conn.execute(
                "SELECT event_key FROM messenger_webhook_events WHERE event_key LIKE 'pool-%-webhook-88403%'"
            ).fetchall()
        }

    assert old_sent not in ids
    assert old_dead not in ids
    assert fresh_sent in ids
    assert fresh_dead in ids
    assert "pool-old-webhook-884031" not in webhooks
    assert "pool-fresh-webhook-884032" in webhooks

@pytest.mark.asyncio
async def test_permanent_provider_rejection_is_not_retried(monkeypatch) -> None:
    item_id = _insert_outbox(
        platform="max",
        user_id=884041,
        event_key="pool-max-permanent-rejection-884041",
    )
    claimed = delivery_pool.claim_stream_head(platform="max", lock_ttl_sec=900)
    assert claimed is not None
    assert claimed.id == item_id

    async def reject(_item: delivery_outbox.ClaimedDelivery) -> None:
        raise MaxProviderRejectedError(
            "MAX provider send_text HTTP 403",
            code="max.send_text.http_403",
        )

    monkeypatch.setattr(delivery_outbox, "_deliver_one", reject)
    await delivery_pool._process_item(claimed)  # noqa: SLF001

    with db() as conn:
        row = conn.execute(
            "SELECT status,attempts,last_error FROM messenger_delivery_outbox WHERE id=?",
            (item_id,),
        ).fetchone()

    assert row["status"] == "rejected"
    assert int(row["attempts"]) == 1
    assert row["last_error"] == "MaxProviderRejectedError:max.send_text.http_403"
    assert delivery_pool.claim_stream_head(platform="max", lock_ttl_sec=900) is None

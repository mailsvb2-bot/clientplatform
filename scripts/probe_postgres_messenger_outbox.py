from __future__ import annotations

import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.db import db
from services.db.runtime import CONFIG
from services.messenger.delivery_outbox import mark_delivery_sent, persist_reply_bundle
from services.messenger.delivery_pool import claim_stream_head
from services.messenger.text_ui import MessengerReply
from services.messenger.webhook_dedupe import claim_inbound_event
from services.schema import init_db


def _cleanup(platform: str, event_keys: list[str]) -> None:
    with db() as conn:
        for event_key in event_keys:
            conn.execute(
                "DELETE FROM messenger_delivery_outbox WHERE platform=? AND event_key=?",
                (platform, event_key),
            )
            conn.execute(
                "DELETE FROM messenger_webhook_events WHERE platform=? AND event_key=?",
                (platform, event_key),
            )


def _persist(platform: str, event_key: str, user_id: int) -> None:
    payload = {"type": "message_new", "object": {"event_id": event_key}}
    assert claim_inbound_event(platform, event_key, payload) is True
    assert persist_reply_bundle(
        platform=platform,
        external_user_id=f"postgres-ci-user-{user_id}",
        canonical_user_id=user_id,
        event_key=event_key,
        replies=[MessengerReply(text=f"postgres durable delivery {event_key}")],
        action="ci_probe",
    ) is True


def main() -> int:
    if not CONFIG.uses_postgres:
        raise SystemExit("POSTGRES_MESSENGER_OUTBOX_FAILED: CLIENTPLATFORM_DB_ENGINE=postgres is required")
    if not (os.getenv("DATABASE_URL") or "").strip():
        raise SystemExit("POSTGRES_MESSENGER_OUTBOX_FAILED: DATABASE_URL is required")

    init_db()
    platform = "vk"
    prefix = f"postgres-ci-{uuid.uuid4().hex}"
    event_keys = [f"{prefix}-clientplatform", f"{prefix}-a2", f"{prefix}-b1"]
    user_a = 9_200_001_311
    user_b = 9_200_001_312
    try:
        _persist(platform, event_keys[0], user_a)
        _persist(platform, event_keys[1], user_a)
        _persist(platform, event_keys[2], user_b)

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(
                executor.map(
                    lambda _: claim_stream_head(platform=platform, lock_ttl_sec=900),
                    range(4),
                )
            )
        claimed = [item for item in results if item is not None and item.event_key in event_keys]
        assert len(claimed) == 2, f"expected two user-stream heads, got {len(claimed)}"
        assert {item.canonical_user_id for item in claimed} == {user_a, user_b}
        assert event_keys[0] in {item.event_key for item in claimed}
        assert event_keys[1] not in {item.event_key for item in claimed}

        for item in claimed:
            mark_delivery_sent(item)

        second_a = claim_stream_head(platform=platform, lock_ttl_sec=900)
        assert second_a is not None
        assert second_a.event_key == event_keys[1]
        assert second_a.canonical_user_id == user_a
        mark_delivery_sent(second_a)

        with db() as conn:
            rows = conn.execute(
                """
                SELECT event_key,status,attempts,sent_at
                FROM messenger_delivery_outbox
                WHERE platform=? AND event_key IN (?,?,?)
                ORDER BY event_key
                """.strip(),
                (platform, *event_keys),
            ).fetchall()
        assert len(rows) == 3
        assert all(str(row["status"]) == "sent" for row in rows)
        assert all(int(row["attempts"] or 0) == 0 for row in rows)
        assert all(str(row["sent_at"] or "") for row in rows)

        print(
            json.dumps(
                {
                    "ok": True,
                    "probe": "postgres_messenger_delivery_pool",
                    "parallel_stream_heads": len(claimed),
                    "ordered_followup": second_a.event_key,
                },
                sort_keys=True,
            )
        )
        print("POSTGRES_MESSENGER_OUTBOX_OK")
        return 0
    finally:
        _cleanup(platform, event_keys)


if __name__ == "__main__":
    raise SystemExit(main())

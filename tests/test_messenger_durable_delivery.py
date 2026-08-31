import importlib
import json

import pytest


def _reload(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("METRO_DB_ENGINE", "sqlite")
    monkeypatch.setenv("METRO_DB_PATH", str(tmp_path / "durable-delivery.db"))
    monkeypatch.setenv("DATABASE_URL", "")

    module_names = [
        "core.paths",
        "services.db.runtime",
        "services.db.core",
        "services.migrations",
        "services.schema_core",
        "services.schema",
        "services.messenger.webhook_dedupe",
        "services.messenger.delivery_outbox",
    ]
    modules = {}
    for name in module_names:
        module = importlib.import_module(name)
        modules[name] = importlib.reload(module)
    modules["services.schema"].init_db()
    return (
        modules["services.messenger.webhook_dedupe"],
        modules["services.messenger.delivery_outbox"],
        modules["services.db.core"],
    )


def _enqueue(dedupe, outbox, *, platform, event_key, user_id, text):
    from services.messenger.text_ui import MessengerReply

    payload = {"type": "message_created", "message": {"id": event_key}}
    assert dedupe.claim_inbound_event(platform, event_key, payload) is True
    assert outbox.persist_reply_bundle(
        platform=platform,
        external_user_id=f"{platform}-{user_id}",
        canonical_user_id=user_id,
        event_key=event_key,
        replies=[MessengerReply(text=text)],
        action="continue",
    ) is True


def test_inbound_claim_can_retry_failed_event(monkeypatch, tmp_path):
    dedupe, _, _ = _reload(monkeypatch, tmp_path)
    payload = {"type": "message_new", "object": {"message": {"id": 1}}}

    assert dedupe.claim_inbound_event("vk", "event-1", payload) is True
    assert dedupe.claim_inbound_event("vk", "event-1", payload) is False

    dedupe.fail_inbound_event("vk", "event-1", payload, "synthetic failure")
    assert dedupe.claim_inbound_event("vk", "event-1", payload) is True


def test_persist_reply_bundle_completes_inbound_and_is_idempotent(monkeypatch, tmp_path):
    dedupe, outbox, db_core = _reload(monkeypatch, tmp_path)
    from services.messenger.text_ui import MessengerReply

    payload = {"type": "message_new", "object": {"message": {"id": 2}}}
    assert dedupe.claim_inbound_event("vk", "event-2", payload) is True

    inserted = outbox.persist_reply_bundle(
        platform="vk",
        external_user_id="vk-42",
        canonical_user_id=42,
        event_key="event-2",
        replies=[MessengerReply(text="hello", meta={"session_id": "7"})],
        action="menu",
    )
    assert inserted is True

    duplicate = outbox.persist_reply_bundle(
        platform="vk",
        external_user_id="vk-42",
        canonical_user_id=42,
        event_key="event-2",
        replies=[MessengerReply(text="hello")],
        action="menu",
    )
    assert duplicate is False

    with db_core.db() as conn:
        event = conn.execute(
            "SELECT status, completed_at FROM messenger_webhook_events WHERE platform=? AND event_key=?",
            ("vk", "event-2"),
        ).fetchone()
        rows = conn.execute(
            "SELECT replies_json, status FROM messenger_delivery_outbox WHERE platform=? AND event_key=?",
            ("vk", "event-2"),
        ).fetchall()

    assert event["status"] == "completed"
    assert event["completed_at"]
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert json.loads(rows[0]["replies_json"])[0]["text"] == "hello"


def test_claim_and_mark_delivery_sent(monkeypatch, tmp_path):
    dedupe, outbox, _ = _reload(monkeypatch, tmp_path)
    _enqueue(dedupe, outbox, platform="max", event_key="max-event-1", user_id=42, text="ready")

    claimed = outbox.claim_due_deliveries(limit=5)
    assert len(claimed) == 1
    assert claimed[0].platform == "max"
    assert outbox.deserialize_replies(claimed[0].replies_json)[0].text == "ready"

    outbox.mark_delivery_sent(claimed[0])
    assert outbox.outbox_snapshot()["sent"] == 1


def test_delivery_failure_is_rescheduled(monkeypatch, tmp_path):
    dedupe, outbox, db_core = _reload(monkeypatch, tmp_path)
    _enqueue(dedupe, outbox, platform="max", event_key="max-event-2", user_id=43, text="retry me")

    item = outbox.claim_due_deliveries(limit=1)[0]
    outbox.reschedule_delivery(item, "transport down")

    with db_core.db() as conn:
        row = conn.execute(
            "SELECT status, attempts, last_error FROM messenger_delivery_outbox WHERE id=?",
            (item.id,),
        ).fetchone()
    assert row["status"] == "retry"
    assert int(row["attempts"]) == 1
    assert "transport down" in row["last_error"]


@pytest.mark.asyncio
async def test_retry_resumes_after_last_completed_reply(monkeypatch, tmp_path):
    dedupe, outbox, db_core = _reload(monkeypatch, tmp_path)
    from services.messenger.text_ui import MessengerReply

    event_key = "max-partial-bundle-1"
    payload = {"type": "message_created", "message": {"id": event_key}}
    assert dedupe.claim_inbound_event("max", event_key, payload) is True
    assert outbox.persist_reply_bundle(
        platform="max",
        external_user_id="max-55",
        canonical_user_id=55,
        event_key=event_key,
        replies=[MessengerReply(text="first"), MessengerReply(text="second")],
        action="menu",
    ) is True

    sent: list[str] = []
    fail_second_once = True

    async def fake_send_reply_bundle(platform, external_user_id, canonical_user_id, replies):
        nonlocal fail_second_once
        assert platform == "max"
        assert external_user_id == "max-55"
        assert canonical_user_id == 55
        assert len(replies) == 1
        text = replies[0].text
        sent.append(text)
        if text == "second" and fail_second_once:
            fail_second_once = False
            raise RuntimeError("synthetic second reply failure")

    monkeypatch.setattr(outbox, "send_reply_bundle", fake_send_reply_bundle)
    first_lease = outbox.claim_due_deliveries(limit=1)[0]
    with pytest.raises(RuntimeError, match="synthetic second reply failure"):
        await outbox._deliver_one(first_lease)

    assert sent == ["first", "second"]
    assert outbox.reply_progress_index(first_lease.id) == 1
    outbox.reschedule_delivery(first_lease, "synthetic second reply failure")
    with db_core.db() as conn:
        conn.execute(
            "UPDATE messenger_delivery_outbox SET available_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", first_lease.id),
        )

    second_lease = outbox.claim_due_deliveries(limit=1)[0]
    await outbox._deliver_one(second_lease)

    assert sent == ["first", "second", "second"]
    assert outbox.reply_progress_index(second_lease.id) == 2
    assert outbox.outbox_snapshot()["sent"] == 1


def test_stale_sending_lease_is_reclaimed_after_worker_crash(monkeypatch, tmp_path):
    dedupe, outbox, db_core = _reload(monkeypatch, tmp_path)
    _enqueue(dedupe, outbox, platform="vk", event_key="vk-crash-1", user_id=44, text="recover")

    first = outbox.claim_due_deliveries(limit=1, lock_ttl_sec=30)
    assert len(first) == 1
    with db_core.db() as conn:
        conn.execute(
            "UPDATE messenger_delivery_outbox SET locked_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", first[0].id),
        )

    recovered = outbox.claim_due_deliveries(limit=1, lock_ttl_sec=30)
    assert len(recovered) == 1
    assert recovered[0].id == first[0].id
    assert recovered[0].lock_token != first[0].lock_token


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["vk", "max"])
async def test_stale_payment_outbox_replay_degrades_when_payment_is_disabled(
    monkeypatch, tmp_path, platform: str
) -> None:
    monkeypatch.setenv("PAYMENT_HTTP_ENABLED", "0")
    dedupe, outbox, db_core = _reload(monkeypatch, tmp_path)
    from services.messenger import reply_dispatcher
    from services.messenger.text_ui import PAYMENT_UNAVAILABLE_TEXT

    class FakeSender:
        def __init__(self) -> None:
            self.text_calls = []

        async def send_text(self, external_user_id, text, **kwargs):
            self.text_calls.append((str(external_user_id), str(text), dict(kwargs)))
            return {"ok": True}

    sender = FakeSender()
    monkeypatch.setattr(reply_dispatcher, "MaxBotSender", lambda: sender if platform == "max" else FakeSender())
    monkeypatch.setattr(reply_dispatcher, "VkBotSender", lambda: sender if platform == "vk" else FakeSender())
    monkeypatch.setattr(
        reply_dispatcher,
        "package_payment_text",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("queued checkout must not rebuild")),
    )

    event_key = f"{platform}-stale-payment-outbox"
    _enqueue(
        dedupe,
        outbox,
        platform=platform,
        event_key=event_key,
        user_id=77,
        text="💳 Старый checkout\n\nhttps://old.example/pay/yookassa?kind=tokens",
    )
    item = outbox.claim_due_deliveries(limit=1)[0]

    await outbox._deliver_one(item)

    assert sender.text_calls
    assert sender.text_calls[-1][1] == PAYMENT_UNAVAILABLE_TEXT
    with db_core.db() as conn:
        row = conn.execute(
            "SELECT status FROM messenger_delivery_outbox WHERE id=?",
            (item.id,),
        ).fetchone()
    assert row["status"] == "sent"

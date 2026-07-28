from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from services import auto_audio


def _candidate(uid: int = 1) -> auto_audio.DueCandidate:
    return {
        "uid": uid,
        "slot": "morning",
        "policy": SimpleNamespace(
            blocked_by_quiet_hours=False,
            timezone="UTC",
            preferred_channel="max",
            resolved_channel="max",
            next_allowed_at=None,
            fallback_used=False,
        ),
        "hm": "08:30",
        "scheduled_now": True,
    }


@pytest.mark.asyncio
async def test_auto_audio_releases_lock_after_transport_os_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[tuple[int, str, str]] = []
    events: list[tuple[str, dict]] = []

    monkeypatch.setattr(auto_audio, "get_index", lambda *_args: 0)
    monkeypatch.setattr(auto_audio, "pick_for_slot", lambda *_args: SimpleNamespace(anchor="clientplatform"))
    monkeypatch.setattr(auto_audio, "was_delivered", lambda *_args: False)
    monkeypatch.setattr(
        auto_audio,
        "acquire_delivery_lock",
        lambda *_args, **_kwargs: SimpleNamespace(acquired=True, stale_reclaimed=False, reason=""),
    )
    monkeypatch.setattr(auto_audio, "create_session", lambda *_args, **_kwargs: 10)

    async def fail_send(*_args, **_kwargs) -> None:
        raise OSError("provider response with secret=must-not-be-recorded")

    async def release(uid: int, kind: str, scheduled_at: str) -> None:
        released.append((uid, kind, scheduled_at))

    monkeypatch.setattr(auto_audio, "_send_pre_prompt", fail_send)
    monkeypatch.setattr(auto_audio, "_unmark_pre_score_lock", release)
    monkeypatch.setattr(
        auto_audio,
        "log_event",
        lambda _uid, name, meta: events.append((name, dict(meta))),
    )

    await auto_audio._process_due_candidate(
        object(),
        _candidate(),
        now_utc=auto_audio.datetime(2026, 7, 20, 8, 31, tzinfo=auto_audio.timezone.utc),
        senders=SimpleNamespace(),
    )

    assert len(released) == 1
    assert events[-1][0] == "auto_audio_error"
    assert events[-1][1]["error_type"] == "OSError"
    assert "must-not-be-recorded" not in str(events)


@pytest.mark.asyncio
async def test_auto_audio_releases_lock_and_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[int] = []

    monkeypatch.setattr(auto_audio, "get_index", lambda *_args: 0)
    monkeypatch.setattr(auto_audio, "pick_for_slot", lambda *_args: SimpleNamespace(anchor="clientplatform"))
    monkeypatch.setattr(auto_audio, "was_delivered", lambda *_args: False)
    monkeypatch.setattr(
        auto_audio,
        "acquire_delivery_lock",
        lambda *_args, **_kwargs: SimpleNamespace(acquired=True, stale_reclaimed=False, reason=""),
    )
    monkeypatch.setattr(auto_audio, "create_session", lambda *_args, **_kwargs: 10)

    async def cancelled(*_args, **_kwargs) -> None:
        raise asyncio.CancelledError

    async def release(uid: int, _kind: str, _scheduled_at: str) -> None:
        released.append(uid)

    monkeypatch.setattr(auto_audio, "_send_pre_prompt", cancelled)
    monkeypatch.setattr(auto_audio, "_unmark_pre_score_lock", release)
    monkeypatch.setattr(auto_audio, "log_event", lambda *_args, **_kwargs: None)

    with pytest.raises(asyncio.CancelledError):
        await auto_audio._process_due_candidate(
            object(),
            _candidate(),
            now_utc=auto_audio.datetime(2026, 7, 20, 8, 31, tzinfo=auto_audio.timezone.utc),
            senders=SimpleNamespace(),
        )

    assert released == [1]


@pytest.mark.asyncio
async def test_auto_audio_worker_pool_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTO_AUDIO_WORKERS", "2")
    active = 0
    max_active = 0
    processed: list[int] = []
    lock = asyncio.Lock()

    async def fake_process(_bot, item, *, now_utc, senders) -> None:
        nonlocal active, max_active
        assert now_utc.tzinfo is not None
        assert senders is not None
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        processed.append(int(item["uid"]))
        async with lock:
            active -= 1

    monkeypatch.setattr(auto_audio, "_process_due_candidate", fake_process)
    candidates = [_candidate(uid) for uid in range(1, 7)]

    await auto_audio._run_candidate_workers(
        object(),
        candidates,
        now_utc=auto_audio.datetime.now(auto_audio.timezone.utc),
        senders=SimpleNamespace(),
    )

    assert sorted(processed) == list(range(1, 7))
    assert max_active == 2

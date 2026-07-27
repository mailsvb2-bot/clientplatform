from __future__ import annotations

import asyncio

import pytest

from services import scheduler
from services.payments import retry_queue


def _reset_scheduler_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler, "_bg_error_count", 0)
    monkeypatch.setattr(scheduler, "_bg_last_error", "")
    monkeypatch.setattr(scheduler, "_bg_last_error_at_monotonic", 0.0)
    monkeypatch.setattr(scheduler, "_reward_timeout_streak", 0)
    monkeypatch.setattr(scheduler, "_reward_timeout_total", 0)


@pytest.mark.asyncio
async def test_reward_timeout_blocks_only_after_configured_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_scheduler_errors(monkeypatch)
    monkeypatch.setenv("REWARD_READY_TIMEOUT_FAILURE_THRESHOLD", "3")

    async def timeout() -> None:
        raise asyncio.TimeoutError

    assert await scheduler._run_protected_tick("RewardEngine.tick", timeout) is False
    assert scheduler._reward_timeout_streak == 1
    assert scheduler._reward_timeout_total == 1
    assert scheduler._bg_error_count == 0
    assert scheduler._bg_last_error == ""

    assert await scheduler._run_protected_tick("RewardEngine.tick", timeout) is False
    assert scheduler._reward_timeout_streak == 2
    assert scheduler._reward_timeout_total == 2
    assert scheduler._bg_error_count == 0

    assert await scheduler._run_protected_tick("RewardEngine.tick", timeout) is False
    assert scheduler._reward_timeout_streak == 3
    assert scheduler._reward_timeout_total == 3
    assert scheduler._bg_error_count == 1
    assert scheduler._bg_last_error == "RewardEngine.tick:TimeoutError"


@pytest.mark.asyncio
async def test_successful_reward_tick_resets_timeout_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_scheduler_errors(monkeypatch)
    monkeypatch.setenv("REWARD_READY_TIMEOUT_FAILURE_THRESHOLD", "3")

    async def timeout() -> None:
        raise asyncio.TimeoutError

    async def success() -> None:
        return None

    assert await scheduler._run_protected_tick("RewardEngine.tick", timeout) is False
    assert scheduler._reward_timeout_streak == 1

    assert await scheduler._run_protected_tick("RewardEngine.tick", success) is True
    assert scheduler._reward_timeout_streak == 0
    assert scheduler._bg_error_count == 0

    assert await scheduler._run_protected_tick("RewardEngine.tick", timeout) is False
    assert scheduler._reward_timeout_streak == 1
    assert scheduler._bg_error_count == 0


@pytest.mark.asyncio
async def test_critical_owner_timeout_still_degrades_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_scheduler_errors(monkeypatch)
    monkeypatch.setenv("REWARD_READY_TIMEOUT_FAILURE_THRESHOLD", "3")

    async def timeout() -> None:
        raise asyncio.TimeoutError

    assert await scheduler._run_protected_tick("engine.tick", timeout) is False
    assert scheduler._bg_error_count == 1
    assert scheduler._bg_last_error == "engine.tick:TimeoutError"
    assert scheduler._reward_timeout_streak == 0


def test_scheduler_snapshot_exposes_reward_timeout_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_scheduler_errors(monkeypatch)
    monkeypatch.setenv("REWARD_READY_TIMEOUT_FAILURE_THRESHOLD", "4")
    monkeypatch.setattr(scheduler, "_reward_timeout_streak", 2)
    monkeypatch.setattr(scheduler, "_reward_timeout_total", 7)
    monkeypatch.setattr(
        retry_queue,
        "payment_retry_health_snapshot",
        lambda: {"payment_retry_active": 0, "payment_retry_dead": 0},
    )

    snapshot = scheduler.scheduler_health_snapshot()

    assert snapshot["reward_engine_timeout_streak"] == 2
    assert snapshot["reward_engine_timeout_total"] == 7
    assert snapshot["reward_engine_timeout_threshold"] == 4

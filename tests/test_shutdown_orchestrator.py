from __future__ import annotations

import asyncio

import pytest

from core.shutdown import ShutdownStep, run_shutdown_steps


@pytest.mark.asyncio
async def test_shutdown_runs_every_step_after_failure() -> None:
    calls: list[str] = []

    async def first() -> None:
        calls.append("first")
        raise RuntimeError("boom")

    async def second() -> None:
        calls.append("second")

    await run_shutdown_steps(
        [
            ShutdownStep("first", first),
            ShutdownStep("second", second),
        ]
    )

    assert calls == ["first", "second"]


@pytest.mark.asyncio
async def test_shutdown_delays_but_preserves_cancellation() -> None:
    calls: list[str] = []

    async def cancelled() -> None:
        calls.append("cancelled")
        raise asyncio.CancelledError

    async def final() -> None:
        calls.append("final")

    with pytest.raises(asyncio.CancelledError):
        await run_shutdown_steps(
            [
                ShutdownStep("cancelled", cancelled),
                ShutdownStep("final", final),
            ]
        )

    assert calls == ["cancelled", "final"]

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Coroutine

from clientplatform.application.dispatch_worker import DispatchBatchResult
from clientplatform.runtime.dispatch_runtime import (
    DispatchRuntime,
    build_dispatch_runtime,
    run_configured_dispatch_tick,
)
from services.bg import tm


Tick = Callable[[DispatchRuntime], Awaitable[DispatchBatchResult]]
TaskFactory = Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class DispatchSchedulerHealth:
    enabled: bool
    running: bool
    iterations: int
    claimed: int
    sent: int
    retried: int
    dead: int
    errors: int
    last_error: str
    last_tick_age_seconds: int


class ClientPlatformDispatchScheduler:
    """Single-owner, non-overlapping scheduler for bounded dispatch batches."""

    def __init__(
        self,
        runtime: DispatchRuntime | None = None,
        *,
        tick: Tick = run_configured_dispatch_tick,
        task_factory: TaskFactory | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._runtime = runtime or build_dispatch_runtime()
        self._tick = tick
        self._task_factory = task_factory or tm().create
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._iterations = 0
        self._claimed = 0
        self._sent = 0
        self._retried = 0
        self._dead = 0
        self._errors = 0
        self._last_error = ""
        self._last_tick_monotonic = 0.0

    def start(self) -> bool:
        if not self._runtime.config.enabled:
            return False
        if self._task is not None and not self._task.done():
            return False
        self._running = True
        self._task = self._task_factory(self._run())
        return True

    async def stop(self) -> None:
        self._running = False
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None

    def _record_tick_error(self, code: str) -> None:
        self._errors += 1
        self._last_error = str(code or "dispatch_tick_failed")[:180]
        self._last_tick_monotonic = time.monotonic()

    async def _run(self) -> None:
        while self._running:
            try:
                result = await asyncio.wait_for(
                    self._tick(self._runtime),
                    timeout=self._runtime.config.tick_timeout_seconds,
                )
                self._iterations += 1
                self._claimed += int(result.claimed)
                self._sent += int(result.sent)
                self._retried += int(result.retried)
                self._dead += int(result.dead)
                self._last_error = ""
                self._last_tick_monotonic = time.monotonic()
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                self._record_tick_error("dispatch_tick_timeout")
            except sqlite3.Error as exc:
                self._record_tick_error(f"{type(exc).__name__}:dispatch_tick_failed")
            except RuntimeError as exc:
                self._record_tick_error(f"{type(exc).__name__}:dispatch_tick_failed")
            except OSError as exc:
                self._record_tick_error(f"{type(exc).__name__}:dispatch_tick_failed")
            except (ValueError, TypeError) as exc:
                self._record_tick_error(f"{type(exc).__name__}:dispatch_tick_failed")
            if self._running:
                await self._sleep(self._runtime.config.interval_seconds)

    def health_snapshot(self) -> DispatchSchedulerHealth:
        now = time.monotonic()
        age = int(now - self._last_tick_monotonic) if self._last_tick_monotonic else 0
        return DispatchSchedulerHealth(
            enabled=self._runtime.config.enabled,
            running=bool(self._task is not None and not self._task.done()),
            iterations=self._iterations,
            claimed=self._claimed,
            sent=self._sent,
            retried=self._retried,
            dead=self._dead,
            errors=self._errors,
            last_error=self._last_error,
            last_tick_age_seconds=age,
        )

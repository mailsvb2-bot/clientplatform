from __future__ import annotations

import asyncio
import heapq
import logging
import os
import sqlite3
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Coroutine, Optional

from core.runtime_env import env_float, env_int

if TYPE_CHECKING:
    from aiogram import Bot

log = logging.getLogger(__name__)


def _coro_name(coro: Awaitable[None]) -> str:
    try:
        return getattr(coro, "__name__")  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass
    try:
        code = getattr(coro, "cr_code", None)
        if code is not None:
            return getattr(code, "co_name", "<coro>")
    except (AttributeError, TypeError, ValueError):
        pass
    return type(coro).__name__


def _tm_create(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    """Create background tasks through TaskManager with a safe fallback."""

    def _attach(task: asyncio.Task[None]) -> asyncio.Task[None]:
        def _done_cb(done: asyncio.Task[None]) -> None:
            try:
                exc = done.exception()
            except asyncio.CancelledError:
                return
            except (asyncio.InvalidStateError, RuntimeError):
                log.error("Background task callback failed task=%s", _coro_name(coro))
                return
            if exc is not None:
                recorded = _record_scheduler_error(_coro_name(coro), exc)
                log_method = log.error if recorded else log.warning
                log_method(
                    "Background task crashed task=%s error_type=%s",
                    _coro_name(coro),
                    type(exc).__name__,
                )

        task.add_done_callback(_done_cb)
        return task

    try:
        from services.bg import tm

        return _attach(tm().create(coro))
    except (ImportError, AttributeError):
        log.error(
            "TaskManager unavailable; falling back to asyncio.create_task task=%s",
            _coro_name(coro),
        )
        return _attach(asyncio.create_task(coro))
    except RuntimeError:
        log.error(
            "TaskManager runtime error; falling back to asyncio.create_task task=%s",
            _coro_name(coro),
        )
        return _attach(asyncio.create_task(coro))
    except OSError:
        log.error(
            "TaskManager OS error; falling back to asyncio.create_task task=%s",
            _coro_name(coro),
        )
        return _attach(asyncio.create_task(coro))


class PreciseScheduler:
    """In-memory precise scheduler based on monotonic loop.time()."""

    def __init__(self) -> None:
        self._queue: list[tuple[float, int, Callable[[], Awaitable[None]]]] = []
        self._cv = asyncio.Condition()
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None
        self._seq = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = _tm_create(self._run())

    async def stop(self) -> None:
        self._running = False
        async with self._cv:
            self._cv.notify_all()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def schedule_at(
        self,
        when_loop_time: float,
        factory: Callable[[], Awaitable[None]],
    ) -> None:
        async with self._cv:
            self._seq += 1
            heapq.heappush(self._queue, (when_loop_time, self._seq, factory))
            self._cv.notify()

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                async with self._cv:
                    while self._running and not self._queue:
                        await self._cv.wait()
                    if not self._running:
                        break
                    when_t, _, factory = self._queue[0]
                    delay = when_t - loop.time()
                    if delay > 0:
                        try:
                            await asyncio.wait_for(self._cv.wait(), timeout=delay)
                            continue
                        except asyncio.TimeoutError:
                            pass
                    heapq.heappop(self._queue)
                await factory()
            except asyncio.CancelledError:
                break
            except (RuntimeError, OSError) as exc:
                _record_scheduler_error("precise_scheduler", exc)
                log.error("PreciseScheduler task failed error_type=%s", type(exc).__name__)
            except Exception as exc:  # validator: allow-wide-except
                _record_scheduler_error("precise_scheduler", exc)
                log.error("PreciseScheduler unexpected failure error_type=%s", type(exc).__name__)


_precise: Optional[PreciseScheduler] = None
_bg_task: Optional[asyncio.Task[None]] = None
_bg_started_at_monotonic: float = 0.0
_bg_iteration_count: int = 0
_bg_error_count: int = 0
_bg_last_error: str = ""
_bg_last_error_at_monotonic: float = 0.0
_bg_last_tick_at_monotonic: float = 0.0
_reward_timeout_streak: int = 0
_reward_timeout_total: int = 0
_owner_tasks: dict[str, asyncio.Task[None]] = {}
_owner_started_at: dict[str, float] = {}


def _reward_timeout_failure_threshold() -> int:
    return env_int(
        "REWARD_READY_TIMEOUT_FAILURE_THRESHOLD",
        3,
        minimum=1,
        maximum=100,
    )


def _record_scheduler_success(source: str) -> None:
    global _reward_timeout_streak
    if source == "RewardEngine.tick":
        _reward_timeout_streak = 0


def _record_scheduler_error(source: str, exc: BaseException) -> bool:
    global _bg_error_count, _bg_last_error, _bg_last_error_at_monotonic
    global _reward_timeout_streak, _reward_timeout_total

    if source == "RewardEngine.tick" and isinstance(exc, asyncio.TimeoutError):
        _reward_timeout_streak += 1
        _reward_timeout_total += 1
        threshold = _reward_timeout_failure_threshold()
        if _reward_timeout_streak < threshold:
            log.warning(
                "RewardEngine timeout tolerated streak=%s threshold=%s",
                _reward_timeout_streak,
                threshold,
            )
            return False
    elif source == "RewardEngine.tick":
        _reward_timeout_streak = 0

    _bg_error_count += 1
    _bg_last_error = f"{source}:{type(exc).__name__}"[:180]
    _bg_last_error_at_monotonic = time.monotonic()
    return True


async def _run_protected_tick(
    name: str,
    factory: Callable[[], Awaitable[object]],
) -> bool:
    try:
        await factory()
        _record_scheduler_success(name)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # validator: allow-wide-except
        recorded = _record_scheduler_error(name, exc)
        log_method = log.error if recorded else log.warning
        log_method("Scheduler protected tick failed: %s error_type=%s", name, type(exc).__name__)
        return False


def _start_owner_tick(
    name: str,
    factory: Callable[[], Awaitable[object]],
) -> asyncio.Task[None] | None:
    existing = _owner_tasks.get(name)
    if existing is not None and not existing.done():
        return None

    timeout_sec = env_float(
        "SCHEDULER_OWNER_MAX_RUNTIME_SEC",
        300.0,
        minimum=5.0,
        maximum=3600.0,
    )

    async def _runner() -> None:
        try:
            await asyncio.wait_for(
                _run_protected_tick(name, factory),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError as exc:
            recorded = _record_scheduler_error(name, exc)
            log_method = log.error if recorded else log.warning
            log_method("Scheduler owner timed out: %s", name)

    task = _tm_create(_runner())
    _owner_tasks[name] = task
    _owner_started_at[name] = time.monotonic()

    def _cleanup(done: asyncio.Task[None]) -> None:
        if _owner_tasks.get(name) is done:
            _owner_tasks.pop(name, None)
            _owner_started_at.pop(name, None)

    task.add_done_callback(_cleanup)
    return task


def get_precise_scheduler() -> PreciseScheduler:
    global _precise
    if _precise is None:
        _precise = PreciseScheduler()
    return _precise


def scheduler_health_snapshot() -> dict[str, bool | int | float | str]:
    precise = get_precise_scheduler()
    precise_task = getattr(precise, "_task", None)
    bg_running = bool(_bg_task is not None and not _bg_task.done())
    precise_running = bool(getattr(precise, "_running", False))
    precise_task_running = bool(precise_task is not None and not precise_task.done())
    queue_size = int(len(getattr(precise, "_queue", ())))
    now_m = time.monotonic()
    active_owner_tasks = [name for name, task in _owner_tasks.items() if not task.done()]
    oldest_owner_age = max(
        (now_m - _owner_started_at.get(name, now_m) for name in active_owner_tasks),
        default=0.0,
    )
    try:
        from services.payments.retry_queue import payment_retry_health_snapshot

        payment_retry = payment_retry_health_snapshot()
    except (ImportError, AttributeError, RuntimeError, OSError, ValueError, TypeError):
        payment_retry = {"payment_retry_active": -1, "payment_retry_dead": -1}
    return {
        "scheduler_loop_task_running": bg_running,
        "precise_scheduler_running": precise_running,
        "precise_scheduler_task_running": precise_task_running,
        "precise_scheduler_queue_size": queue_size,
        "scheduler_loop_started": bool(_bg_started_at_monotonic > 0),
        "scheduler_loop_uptime_sec": (
            int(now_m - _bg_started_at_monotonic) if _bg_started_at_monotonic else 0
        ),
        "scheduler_loop_iterations": int(_bg_iteration_count),
        "scheduler_loop_error_count": int(_bg_error_count),
        "scheduler_loop_last_error": _bg_last_error,
        "scheduler_loop_last_error_age_sec": (
            int(now_m - _bg_last_error_at_monotonic) if _bg_last_error_at_monotonic else 0
        ),
        "scheduler_loop_last_tick_age_sec": (
            int(now_m - _bg_last_tick_at_monotonic) if _bg_last_tick_at_monotonic else 0
        ),
        "scheduler_owner_tasks_running": len(active_owner_tasks),
        "scheduler_owner_oldest_age_sec": int(oldest_owner_age),
        "reward_engine_timeout_streak": int(_reward_timeout_streak),
        "reward_engine_timeout_total": int(_reward_timeout_total),
        "reward_engine_timeout_threshold": int(_reward_timeout_failure_threshold()),
        **payment_retry,
    }


async def _run_ux_guard_tick() -> None:
    from services.ai.ux_guard import analyze
    from services.db import get_db

    app_env = (os.getenv("APP_ENV") or "dev").lower()
    timeout = 2.0 if app_env == "prod" else 5.0

    def _run() -> None:
        with get_db() as conn:
            try:
                conn.execute("PRAGMA query_only=ON")
            except (sqlite3.Error, OSError, RuntimeError):
                pass
            analyze(conn)

    await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout)


async def _safe_ux_guard_tick() -> None:
    try:
        await _run_ux_guard_tick()
    except asyncio.TimeoutError:
        log.warning("UX guard timed out")
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            log.debug("UX guard skipped until schema is ready", exc_info=True)
            return
        log.error("UX guard DB operational error")
    except (sqlite3.Error, RuntimeError, OSError, ValueError, TypeError, AttributeError, KeyError):
        log.error("UX guard failed")
    except Exception:  # validator: allow-wide-except
        log.error("UX guard unexpected failure")


async def _run_growth_conversion_bridge_tick() -> None:
    from services.growth_conversion_event_bridge import run_event_conversion_bridge_safe

    batch_size = env_int(
        "GROWTH_CONVERSION_BRIDGE_BATCH_SIZE",
        100,
        minimum=1,
        maximum=10_000,
    )
    result = await asyncio.to_thread(run_event_conversion_bridge_safe, batch_size=batch_size)
    if result.error:
        log.warning("Growth conversion bridge degraded: %s", result.error)


async def _run_payment_reconciliation_retry_tick() -> None:
    from services.payments.retry_queue import run_payment_retry_batch

    result = await asyncio.to_thread(run_payment_retry_batch)
    if result.dead:
        log.error(
            "Payment reconciliation retries dead-lettered: claimed=%s completed=%s rescheduled=%s dead=%s",
            result.claimed,
            result.completed,
            result.rescheduled,
            result.dead,
        )
    elif result.claimed:
        log.info(
            "Payment reconciliation retry tick: claimed=%s completed=%s rescheduled=%s",
            result.claimed,
            result.completed,
            result.rescheduled,
        )


def _start_telegram_owner_ticks(
    bot: "Bot | None",
    *,
    auto_audio_tick: Callable[["Bot"], Awaitable[object]],
    engine_tick: Callable[["Bot"], Awaitable[object]],
) -> None:
    """Start only the legacy Telegram-dependent owners when a Bot exists."""

    if bot is None:
        return
    _start_owner_tick("auto_audio.tick", lambda: auto_audio_tick(bot))
    _start_owner_tick("engine.tick", lambda: engine_tick(bot))


async def _background_loop(bot: "Bot | None") -> None:
    from core.ai.reward_engine import compute_and_store_rewards
    from core.engine import engine
    from core.runtime.self_healing import SelfHealingEngine
    from services.auto_audio import tick as auto_audio_tick

    global _bg_iteration_count, _bg_last_tick_at_monotonic

    self_heal = SelfHealingEngine()
    app_env = (os.getenv("APP_ENV") or "dev").lower()
    heal_interval = env_float(
        "SELF_HEAL_INTERVAL_SEC",
        5.0,
        minimum=1.0,
        maximum=3600.0,
    )
    reward_interval = env_float(
        "REWARD_TICK_INTERVAL_SEC",
        60.0,
        minimum=10.0,
        maximum=86_400.0,
    )
    growth_bridge_interval = env_float(
        "GROWTH_CONVERSION_BRIDGE_INTERVAL_SEC",
        60.0,
        minimum=10.0,
        maximum=86_400.0,
    )
    payment_retry_interval = env_float(
        "PAYMENT_RETRY_INTERVAL_SEC",
        30.0,
        minimum=5.0,
        maximum=86_400.0,
    )
    ux_guard_interval = env_float(
        "UX_GUARD_INTERVAL_SEC",
        60.0 if app_env == "prod" else 10.0,
        minimum=10.0,
        maximum=86_400.0,
    )
    reward_timeout = 5.0 if app_env == "prod" else 15.0
    reward_window_sec = env_int(
        "REWARD_WINDOW_SEC",
        3600,
        minimum=60,
        maximum=31 * 24 * 60 * 60,
    )
    reward_lookback_h = env_int(
        "REWARD_LOOKBACK_H",
        24,
        minimum=1,
        maximum=24 * 365,
    )

    last_heal = 0.0
    last_ux_guard = 0.0
    last_reward = 0.0
    last_growth_conversion_bridge = 0.0
    last_payment_reconciliation_retry = 0.0

    while True:
        await asyncio.sleep(1)
        _bg_iteration_count += 1
        _bg_last_tick_at_monotonic = time.monotonic()
        now_m = _bg_last_tick_at_monotonic

        if now_m - last_heal >= heal_interval:
            last_heal = now_m
            _start_owner_tick(
                "SelfHealingEngine.tick",
                lambda: asyncio.to_thread(self_heal.tick),
            )

        _start_telegram_owner_ticks(
            bot,
            auto_audio_tick=auto_audio_tick,
            engine_tick=engine.tick,
        )

        if now_m - last_reward >= reward_interval:
            last_reward = now_m

            async def _reward_tick() -> None:
                reward_task = asyncio.to_thread(
                    compute_and_store_rewards,
                    reward_window_sec,
                    lookback_hours=reward_lookback_h,
                )
                await asyncio.wait_for(reward_task, timeout=reward_timeout)

            _start_owner_tick("RewardEngine.tick", _reward_tick)

        if now_m - last_growth_conversion_bridge >= growth_bridge_interval:
            last_growth_conversion_bridge = now_m
            _start_owner_tick(
                "GrowthConversionBridge.tick",
                _run_growth_conversion_bridge_tick,
            )

        if now_m - last_payment_reconciliation_retry >= payment_retry_interval:
            last_payment_reconciliation_retry = now_m
            _start_owner_tick(
                "PaymentReconciliationRetry.tick",
                _run_payment_reconciliation_retry_tick,
            )

        if now_m - last_ux_guard >= ux_guard_interval:
            last_ux_guard = now_m
            _start_owner_tick("UXGuard.tick", _safe_ux_guard_tick)


def start_scheduler(bot: "Bot | None") -> None:
    global _bg_task, _bg_started_at_monotonic

    async def runner_bg() -> None:
        await get_precise_scheduler().start()
        await _background_loop(bot)

    if not _bg_task or _bg_task.done():
        _bg_started_at_monotonic = time.monotonic()
        _bg_task = _tm_create(runner_bg())


async def stop_scheduler() -> None:
    global _bg_task, _bg_started_at_monotonic
    global _reward_timeout_streak

    if _bg_task and not _bg_task.done():
        _bg_task.cancel()
    if _bg_task:
        try:
            await _bg_task
        except asyncio.CancelledError:
            pass

    owner_tasks = [task for task in _owner_tasks.values() if not task.done()]
    for task in owner_tasks:
        task.cancel()
    if owner_tasks:
        await asyncio.gather(*owner_tasks, return_exceptions=True)
    _owner_tasks.clear()
    _owner_started_at.clear()

    _bg_task = None
    _bg_started_at_monotonic = 0.0
    _reward_timeout_streak = 0
    await get_precise_scheduler().stop()


# Resume pending/running jobs on startup.
# Enqueue idempotency is enforced by jobs.job_key UNIQUE index + INSERT OR IGNORE.

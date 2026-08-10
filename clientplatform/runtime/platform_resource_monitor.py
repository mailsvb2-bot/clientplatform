from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from aiogram.exceptions import TelegramAPIError

from config.settings import ADMIN_IDS
from core.runtime_env import env_int
from core.task_manager import TaskManager
from services.db import get_db, get_db_ro
from services.platform_resource_limits import (
    crossed_thresholds,
    current_levels,
    get_platform_resource_snapshot,
    render_threshold_notification,
)


log = logging.getLogger(__name__)
_STATE_KEY = "clientplatform:platform_resource_monitor:visual_gateway"
_task_manager = TaskManager()
_task: asyncio.Task[None] | None = None
_last_tick_monotonic: float | None = None
_last_error = ""


def _interval_seconds() -> int:
    return env_int(
        "CLIENTPLATFORM_RESOURCE_MONITOR_INTERVAL_SEC",
        300,
        minimum=60,
        maximum=3600,
    )


def _load_state() -> dict[str, Any]:
    with get_db_ro() as conn:
        row = conn.execute(
            "SELECT value FROM engine_state WHERE key=? LIMIT 1",
            (_STATE_KEY,),
        ).fetchone()
    if row is None:
        return {}
    raw = row["value"] if hasattr(row, "keys") else row[0]
    try:
        value = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _save_state(value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO engine_state(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (_STATE_KEY, payload, int(time.time())),
        )


async def _send_superadmins(bot: Any, text: str) -> int:
    delivered = 0
    for admin_id in sorted({int(value) for value in ADMIN_IDS or []}):
        try:
            await bot.send_message(admin_id, text)
        except (TelegramAPIError, asyncio.TimeoutError):
            log.warning(
                "Failed to send platform resource alert to superadmin=%s",
                admin_id,
                exc_info=True,
            )
            continue
        delivered += 1
    return delivered


def _telemetry_warning(error_code: str) -> str:
    return (
        "⚠️ ClientPlatform: контроль лимитов Visual Creative недоступен\n\n"
        f"Причина: {error_code or 'unknown'}\n\n"
        "Что делать:\n"
        "1. Проверить, что контейнер visual-creative-gateway запущен.\n"
        "2. Проверить защищённый endpoint /v1/usage.\n"
        "3. Пока телеметрия не восстановлена, сверять расход и квоты в Yandex Cloud вручную."
    )


async def _tick(bot: Any) -> None:
    global _last_error
    global _last_tick_monotonic

    snapshot = await asyncio.to_thread(get_platform_resource_snapshot)
    state = await asyncio.to_thread(_load_state)
    today = datetime.now(timezone.utc).date().isoformat()

    if not snapshot.telemetry_available:
        error_code = snapshot.error_code or "visual_gateway_usage_unavailable"
        already_reported = (
            str(state.get("telemetry_day") or "") == today
            and str(state.get("telemetry_error") or "") == error_code
        )
        if not already_reported:
            delivered = await _send_superadmins(bot, _telemetry_warning(error_code))
            if delivered:
                state["telemetry_day"] = today
                state["telemetry_error"] = error_code
                await asyncio.to_thread(_save_state, state)
        _last_error = error_code
        _last_tick_monotonic = time.monotonic()
        return

    day = snapshot.day_utc or today
    previous_levels = (
        state.get("levels")
        if str(state.get("day_utc") or "") == day and isinstance(state.get("levels"), dict)
        else {}
    )
    crossed = crossed_thresholds(snapshot, previous_levels)
    levels = current_levels(snapshot)

    if crossed:
        message = render_threshold_notification(snapshot, crossed)
        delivered = await _send_superadmins(bot, message)
        if not delivered:
            _last_error = "platform_resource_alert_delivery_failed"
            _last_tick_monotonic = time.monotonic()
            return

    await asyncio.to_thread(
        _save_state,
        {
            "day_utc": day,
            "levels": levels,
            "telemetry_day": "",
            "telemetry_error": "",
        },
    )
    _last_error = ""
    _last_tick_monotonic = time.monotonic()


async def _monitor_loop(bot: Any) -> None:
    try:
        while True:
            try:
                await _tick(bot)
            except (OSError, RuntimeError, TypeError, ValueError):
                global _last_error
                global _last_tick_monotonic
                _last_error = "platform_resource_monitor_tick_failed"
                _last_tick_monotonic = time.monotonic()
                log.exception("ClientPlatform platform resource monitor tick failed")
            await asyncio.sleep(_interval_seconds())
    except asyncio.CancelledError:
        raise


async def start_platform_resource_monitor(bot: Any) -> None:
    global _task
    if _task is not None and not _task.done():
        return
    _task = _task_manager.create(
        _monitor_loop(bot),
        name="clientplatform-platform-resource-monitor",
    )


async def stop_platform_resource_monitor(bot: Any) -> None:
    del bot
    global _task
    task = _task
    _task = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return


def platform_resource_monitor_snapshot() -> dict[str, Any]:
    age = (
        None
        if _last_tick_monotonic is None
        else max(0.0, time.monotonic() - _last_tick_monotonic)
    )
    return {
        "running": _task is not None and not _task.done(),
        "last_tick_age_sec": None if age is None else round(age, 3),
        "last_error": _last_error,
    }


__all__ = [
    "platform_resource_monitor_snapshot",
    "start_platform_resource_monitor",
    "stop_platform_resource_monitor",
]

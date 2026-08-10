from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterable

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
        60,
        minimum=60,
        maximum=3600,
    )


def _superadmin_ids() -> tuple[int, ...]:
    return tuple(sorted({int(value) for value in ADMIN_IDS or []}))


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


def _recipient_ids(values: Iterable[object]) -> tuple[int, ...]:
    configured = set(_superadmin_ids())
    recipients: set[int] = set()
    for value in values:
        try:
            candidate = int(value)
        except (TypeError, ValueError):
            continue
        if candidate in configured:
            recipients.add(candidate)
    return tuple(sorted(recipients))


async def _send_superadmins(
    bot: Any,
    text: str,
    *,
    recipient_ids: Iterable[object] | None = None,
) -> tuple[set[int], set[int]]:
    targets = _superadmin_ids() if recipient_ids is None else _recipient_ids(recipient_ids)
    delivered: set[int] = set()
    failed: set[int] = set()
    for admin_id in targets:
        try:
            await bot.send_message(admin_id, text)
        except TelegramAPIError:
            log.warning(
                "Failed to send platform resource alert to superadmin=%s",
                admin_id,
                exc_info=True,
            )
            failed.add(admin_id)
            continue
        except asyncio.TimeoutError:
            log.warning(
                "Timed out sending platform resource alert to superadmin=%s",
                admin_id,
                exc_info=True,
            )
            failed.add(admin_id)
            continue
        delivered.add(admin_id)
    return delivered, failed


def _telemetry_warning(error_code: str) -> str:
    return (
        "⚠️ ClientPlatform: контроль лимитов Visual Creative недоступен\n\n"
        f"Причина: {error_code or 'unknown'}\n\n"
        "Что делать:\n"
        "1. Проверить, что контейнер visual-creative-gateway запущен.\n"
        "2. Проверить защищённый endpoint /v1/usage.\n"
        "3. Пока телеметрия не восстановлена, сверять расход и квоты в Yandex Cloud вручную."
    )


async def _deliver_telemetry_warning(
    bot: Any,
    *,
    state: dict[str, Any],
    today: str,
    error_code: str,
) -> None:
    pending = state.get("telemetry_pending")
    same_pending = (
        isinstance(pending, dict)
        and str(pending.get("day") or "") == today
        and str(pending.get("error") or "") == error_code
    )
    if same_pending:
        message = str(pending.get("message") or _telemetry_warning(error_code))
        recipients = _recipient_ids(pending.get("pending_admin_ids") or [])
    else:
        already_reported = (
            str(state.get("telemetry_day") or "") == today
            and str(state.get("telemetry_error") or "") == error_code
        )
        if already_reported:
            return
        message = _telemetry_warning(error_code)
        recipients = _superadmin_ids()

    if recipients:
        _delivered, failed = await _send_superadmins(
            bot,
            message,
            recipient_ids=recipients,
        )
        if failed:
            state["telemetry_pending"] = {
                "day": today,
                "error": error_code,
                "message": message,
                "pending_admin_ids": sorted(failed),
            }
            await asyncio.to_thread(_save_state, state)
            return

    state.pop("telemetry_pending", None)
    state["telemetry_day"] = today
    state["telemetry_error"] = error_code
    await asyncio.to_thread(_save_state, state)


async def _finish_pending_threshold(
    bot: Any,
    *,
    state: dict[str, Any],
) -> bool:
    pending = state.get("threshold_pending")
    if not isinstance(pending, dict):
        return True

    recipients = _recipient_ids(pending.get("pending_admin_ids") or [])
    if recipients:
        message = str(pending.get("message") or "").strip()
        if not message:
            state.pop("threshold_pending", None)
            return True
        _delivered, failed = await _send_superadmins(
            bot,
            message,
            recipient_ids=recipients,
        )
        if failed:
            pending["pending_admin_ids"] = sorted(failed)
            state["threshold_pending"] = pending
            await asyncio.to_thread(_save_state, state)
            return False

    target_levels = pending.get("target_levels")
    if isinstance(target_levels, dict):
        state["levels"] = {
            str(key): int(value)
            for key, value in target_levels.items()
            if str(value).lstrip("-").isdigit()
        }
    state.pop("threshold_pending", None)
    await asyncio.to_thread(_save_state, state)
    return True


async def _tick(bot: Any) -> None:
    global _last_error
    global _last_tick_monotonic

    snapshot = await asyncio.to_thread(get_platform_resource_snapshot)
    state = await asyncio.to_thread(_load_state)
    today = datetime.now(timezone.utc).date().isoformat()

    if not snapshot.telemetry_available:
        error_code = snapshot.error_code or "visual_gateway_usage_unavailable"
        await _deliver_telemetry_warning(
            bot,
            state=state,
            today=today,
            error_code=error_code,
        )
        _last_error = error_code
        _last_tick_monotonic = time.monotonic()
        return

    if not await _finish_pending_threshold(bot, state=state):
        _last_error = "platform_resource_alert_delivery_failed"
        _last_tick_monotonic = time.monotonic()
        return

    day = snapshot.day_utc or today
    if str(state.get("day_utc") or "") != day:
        state = {
            "day_utc": day,
            "levels": {},
            "telemetry_day": "",
            "telemetry_error": "",
        }

    previous_levels = state.get("levels") if isinstance(state.get("levels"), dict) else {}
    crossed = crossed_thresholds(snapshot, previous_levels)
    levels = current_levels(snapshot)

    if crossed:
        message = render_threshold_notification(snapshot, crossed)
        _delivered, failed = await _send_superadmins(bot, message)
        if failed:
            state["threshold_pending"] = {
                "day": day,
                "message": message,
                "pending_admin_ids": sorted(failed),
                "target_levels": levels,
            }
            await asyncio.to_thread(_save_state, state)
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


def _record_tick_failure() -> None:
    global _last_error
    global _last_tick_monotonic
    _last_error = "platform_resource_monitor_tick_failed"
    _last_tick_monotonic = time.monotonic()
    log.exception("ClientPlatform platform resource monitor tick failed")


async def _monitor_loop(bot: Any) -> None:
    try:
        while True:
            try:
                await _tick(bot)
            except Exception:  # validator: allow-wide-except
                # A process-owned monitor must survive one transient database,
                # driver or transport failure and retry on the next tick.
                _record_tick_failure()
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

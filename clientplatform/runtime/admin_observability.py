from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from clientplatform.application.admin_ops import (
    purge_old_interaction_metrics,
    refresh_interaction_alerts,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from core.runtime_env import env_float, env_int
from core.task_manager import TaskManager
from core.telegram_multi_egress import (
    telegram_egress_snapshot,
    telegram_readiness_required,
    telegram_redundancy_required,
)
from services.db import get_db_ro


log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AdminObservabilitySnapshot:
    task_running: bool
    last_tick_age_sec: float | None
    last_error: str
    open_alerts: int
    monitored_businesses: int


_task_manager = TaskManager()
_task: asyncio.Task[None] | None = None
_last_tick_monotonic: float | None = None
_last_error = ""
_open_alerts = 0
_monitored_businesses = 0
_health_installed = False


def _owner_contexts() -> list[TenantContext]:
    with get_db_ro() as conn:
        rows = conn.execute(
            """
            SELECT bm.business_id, bm.user_id, bm.id, bm.role
            FROM business_members bm
            JOIN businesses b ON b.id=bm.business_id
            WHERE bm.status='active' AND bm.role='owner' AND b.status='active'
            ORDER BY bm.business_id, bm.created_at, bm.id
            """
        ).fetchall()
    contexts: list[TenantContext] = []
    for row in rows:
        if hasattr(row, "keys"):
            business_id = row["business_id"]
            user_id = row["user_id"]
            membership_id = row["id"]
            role = row["role"]
        else:
            business_id, user_id, membership_id, role = row
        contexts.append(
            TenantContext(
                business_id=str(business_id),
                user_id=int(user_id),
                membership_id=str(membership_id),
                role=PlatformRole(str(role)),
            )
        )
    return contexts


def _monitor_interval() -> float:
    return env_float(
        "CLIENTPLATFORM_ADMIN_MONITOR_INTERVAL_SEC",
        60.0,
        minimum=10.0,
        maximum=3600.0,
    )


def _metric_retention_days() -> int:
    return env_int(
        "CLIENTPLATFORM_ADMIN_METRIC_RETENTION_DAYS",
        14,
        minimum=1,
        maximum=365,
    )


def _monitor_readiness_required() -> bool:
    return (
        env_int(
            "CLIENTPLATFORM_REQUIRE_ADMIN_OBSERVABILITY_READY",
            0,
            minimum=0,
            maximum=1,
        )
        == 1
    )


async def _tick() -> None:
    global _last_tick_monotonic
    global _last_error
    global _open_alerts
    global _monitored_businesses

    egress = telegram_egress_snapshot()
    contexts = await asyncio.to_thread(_owner_contexts)
    alert_count = 0
    for actor in contexts:
        alerts = await asyncio.to_thread(
            refresh_interaction_alerts,
            actor=actor,
            route_redundant=egress.egress_redundant,
        )
        alert_count += len(alerts)
        for alert in alerts:
            log.warning(
                "ClientPlatform admin alert business_id=%s kind=%s severity=%s occurrences=%s message=%s",
                actor.business_id,
                alert.kind,
                alert.severity,
                alert.occurrences,
                alert.message,
            )
    _monitored_businesses = len(contexts)
    _open_alerts = alert_count
    _last_error = ""
    _last_tick_monotonic = time.monotonic()


async def _monitor_loop() -> None:
    try:
        await asyncio.to_thread(
            purge_old_interaction_metrics,
            retention_days=_metric_retention_days(),
        )
        while True:
            try:
                await _tick()
            except Exception as exc:  # validator: allow-wide-except
                global _last_error
                global _last_tick_monotonic
                _last_error = f"{type(exc).__name__}:{exc}"
                _last_tick_monotonic = time.monotonic()
                log.exception("ClientPlatform admin observability tick failed")
            await asyncio.sleep(_monitor_interval())
    except asyncio.CancelledError:
        raise


async def start_admin_observability(_bot: Any) -> None:
    global _task
    if _task is not None and not _task.done():
        return
    _task = _task_manager.create(
        _monitor_loop(),
        name="clientplatform-admin-observability",
    )


async def stop_admin_observability(_bot: Any) -> None:
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


def admin_observability_snapshot() -> AdminObservabilitySnapshot:
    task = _task
    age = (
        None
        if _last_tick_monotonic is None
        else max(0.0, time.monotonic() - _last_tick_monotonic)
    )
    return AdminObservabilitySnapshot(
        task_running=task is not None and not task.done(),
        last_tick_age_sec=None if age is None else round(age, 3),
        last_error=_last_error,
        open_alerts=_open_alerts,
        monitored_businesses=_monitored_businesses,
    )


def _append_error(payload: dict[str, Any], value: str) -> None:
    current = str(payload.get("error") or "").strip()
    payload["error"] = f"{current};{value}" if current else value


def install_health_contract() -> None:
    """Expose Telegram polling/egress state and gate readiness when configured."""

    global _health_installed
    if _health_installed:
        return

    from runtime import health_server

    original_health = health_server.build_health_payload
    original_readiness = health_server.build_readiness_payload

    def health_payload() -> tuple[dict[str, Any], int]:
        payload, status = original_health()
        egress = telegram_egress_snapshot()
        observability = admin_observability_snapshot()
        payload.update(
            {
                "clientplatform_telegram_ui_mode": egress.ui_mode,
                "clientplatform_telegram_polling_mode": egress.polling_mode,
                "clientplatform_telegram_ui_route": egress.ui_route,
                "clientplatform_telegram_polling_route": egress.polling_route,
                "clientplatform_telegram_route_pool_size": egress.route_pool_size,
                "clientplatform_telegram_egress_redundant": egress.egress_redundant,
                "clientplatform_telegram_polling_ready": egress.polling_ready,
                "clientplatform_telegram_polling_in_flight": egress.polling_in_flight,
                "clientplatform_telegram_polling_last_success_age_sec": (
                    egress.polling_last_success_age_sec
                ),
                "clientplatform_telegram_ui_last_success_age_sec": (
                    egress.ui_last_success_age_sec
                ),
                "clientplatform_telegram_ui_failures": egress.ui_failures,
                "clientplatform_telegram_polling_failures": egress.polling_failures,
                "clientplatform_admin_observability_running": (
                    observability.task_running
                ),
                "clientplatform_admin_observability_last_tick_age_sec": (
                    observability.last_tick_age_sec
                ),
                "clientplatform_admin_observability_last_error": (
                    observability.last_error
                ),
                "clientplatform_admin_open_alerts": observability.open_alerts,
                "clientplatform_admin_monitored_businesses": (
                    observability.monitored_businesses
                ),
            }
        )
        return payload, status

    def readiness_payload() -> tuple[dict[str, Any], int]:
        payload, status = original_readiness()
        egress = telegram_egress_snapshot()
        observability = admin_observability_snapshot()
        require_polling = telegram_readiness_required()
        require_redundancy = telegram_redundancy_required()
        polling_ok = not require_polling or egress.polling_ready
        redundancy_ok = not require_redundancy or egress.egress_redundant
        require_monitor = _monitor_readiness_required()
        monitor_running_ok = (
            observability.task_running and not observability.last_error
        )
        monitor_ok = not require_monitor or monitor_running_ok
        payload.update(
            {
                "clientplatform_telegram_polling_ready": egress.polling_ready,
                "clientplatform_telegram_polling_in_flight": egress.polling_in_flight,
                "clientplatform_telegram_polling_required": require_polling,
                "clientplatform_telegram_egress_redundant": egress.egress_redundant,
                "clientplatform_telegram_redundancy_required": require_redundancy,
                "clientplatform_telegram_route_pool_size": egress.route_pool_size,
                "clientplatform_admin_observability_ready": monitor_running_ok,
                "clientplatform_admin_observability_required": require_monitor,
                "clientplatform_admin_open_alerts": observability.open_alerts,
            }
        )
        if not polling_ok:
            _append_error(payload, "telegram_polling:not_ready")
        if not redundancy_ok:
            _append_error(payload, "telegram_egress:not_redundant")
        if not monitor_ok:
            _append_error(payload, "clientplatform_admin_observability:not_ready")
        if not polling_ok or not redundancy_ok or not monitor_ok:
            payload["ok"] = False
            return payload, 500
        return payload, status

    health_server.build_health_payload = health_payload
    health_server.build_readiness_payload = readiness_payload
    _health_installed = True


__all__ = [
    "AdminObservabilitySnapshot",
    "admin_observability_snapshot",
    "install_health_contract",
    "start_admin_observability",
    "stop_admin_observability",
]

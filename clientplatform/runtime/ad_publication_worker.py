from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from clientplatform.application.ad_connections import (
    ad_connections_enabled,
    process_one_ad_publication,
    yandex_direct_provider_configured,
)
from clientplatform.application.ad_spend_operations import (
    process_one_ad_spend_operation,
)
from clientplatform.application.ad_spend_runtime import (
    production_pre_mutation_guard,
    provider_report_date,
    sweep_active_ad_spend_authorizations,
)
from core.task_manager import TaskManager

log = logging.getLogger(__name__)


_HEALTH: dict[str, Any] = {
    "running": False,
    "iterations": 0,
    "publication_processed": 0,
    "spend_operations_processed": 0,
    "spend_guard_scanned": 0,
    "spend_guard_allowed": 0,
    "spend_guard_stops_queued": 0,
    "spend_guard_failed_closed": 0,
    "errors": 0,
    "last_error": "",
    "last_tick_monotonic": 0.0,
}


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


def _record_tick() -> None:
    _HEALTH["iterations"] = int(_HEALTH["iterations"]) + 1
    _HEALTH["last_tick_monotonic"] = time.monotonic()
    _HEALTH["last_error"] = ""


def _record_error(exc: BaseException) -> None:
    _HEALTH["errors"] = int(_HEALTH["errors"]) + 1
    _HEALTH["last_error"] = type(exc).__name__
    _HEALTH["last_tick_monotonic"] = time.monotonic()


def _configuration_error() -> str:
    try:
        provider_report_date(now=datetime.now(timezone.utc))
    except OSError as exc:
        return type(exc).__name__
    except RuntimeError as exc:
        return type(exc).__name__
    except ValueError as exc:
        return type(exc).__name__
    return ""


def ad_publication_worker_health_snapshot() -> dict[str, Any]:
    configured = ad_connections_enabled() and yandex_direct_provider_configured()
    configuration_error = _configuration_error() if configured else ""
    last_tick = float(_HEALTH.get("last_tick_monotonic") or 0.0)
    age = 0 if last_tick <= 0 else max(0, int(time.monotonic() - last_tick))
    return {
        "clientplatform_ad_runtime_configured": configured,
        "clientplatform_ad_runtime_configuration_ok": not configuration_error,
        "clientplatform_ad_runtime_configuration_error": configuration_error,
        "clientplatform_ad_runtime_running": bool(_HEALTH["running"]),
        "clientplatform_ad_runtime_iterations": int(_HEALTH["iterations"]),
        "clientplatform_ad_publication_processed": int(
            _HEALTH["publication_processed"]
        ),
        "clientplatform_ad_spend_operations_processed": int(
            _HEALTH["spend_operations_processed"]
        ),
        "clientplatform_ad_spend_guard_scanned": int(
            _HEALTH["spend_guard_scanned"]
        ),
        "clientplatform_ad_spend_guard_allowed": int(
            _HEALTH["spend_guard_allowed"]
        ),
        "clientplatform_ad_spend_guard_stops_queued": int(
            _HEALTH["spend_guard_stops_queued"]
        ),
        "clientplatform_ad_spend_guard_failed_closed": int(
            _HEALTH["spend_guard_failed_closed"]
        ),
        "clientplatform_ad_runtime_errors": int(_HEALTH["errors"]),
        "clientplatform_ad_runtime_last_error": str(_HEALTH["last_error"]),
        "clientplatform_ad_runtime_last_tick_age_seconds": age,
    }


@dataclass(slots=True)
class AdPublicationWorker:
    task_manager: TaskManager
    interval_seconds: float = 2.0
    guard_interval_seconds: float = 5.0
    _task: asyncio.Task[None] | None = None
    _running: bool = False

    @classmethod
    def from_environment(cls, *, task_manager: TaskManager) -> "AdPublicationWorker":
        return cls(
            task_manager=task_manager,
            interval_seconds=_env_float(
                "CLIENTPLATFORM_AD_PUBLICATION_INTERVAL_SEC",
                2.0,
                minimum=0.25,
                maximum=60.0,
            ),
            guard_interval_seconds=_env_float(
                "CLIENTPLATFORM_AD_SPEND_GUARD_INTERVAL_SEC",
                5.0,
                minimum=1.0,
                maximum=60.0,
            ),
        )

    def start(self) -> bool:
        if self._running:
            return False
        if not ad_connections_enabled() or not yandex_direct_provider_configured():
            return False
        self._running = True
        _HEALTH["running"] = True
        self._task = self.task_manager.create(
            self._run(),
            name="clientplatform-ad-publication-worker",
        )
        return True

    async def stop(self) -> None:
        self._running = False
        _HEALTH["running"] = False
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        next_guard_at = 0.0
        while self._running:
            try:
                publication = await asyncio.to_thread(process_one_ad_publication)
                if publication is not None:
                    _HEALTH["publication_processed"] = (
                        int(_HEALTH["publication_processed"]) + 1
                    )

                current_monotonic = time.monotonic()
                if current_monotonic >= next_guard_at:
                    sweep = await asyncio.to_thread(
                        sweep_active_ad_spend_authorizations
                    )
                    _HEALTH["spend_guard_scanned"] = (
                        int(_HEALTH["spend_guard_scanned"]) + sweep.scanned
                    )
                    _HEALTH["spend_guard_allowed"] = (
                        int(_HEALTH["spend_guard_allowed"]) + sweep.allowed
                    )
                    _HEALTH["spend_guard_stops_queued"] = (
                        int(_HEALTH["spend_guard_stops_queued"])
                        + sweep.stops_queued
                    )
                    _HEALTH["spend_guard_failed_closed"] = (
                        int(_HEALTH["spend_guard_failed_closed"])
                        + sweep.failed_closed
                    )
                    next_guard_at = current_monotonic + self.guard_interval_seconds

                spend_operation = await asyncio.to_thread(
                    process_one_ad_spend_operation,
                    pre_mutation_guard=production_pre_mutation_guard,
                )
                if spend_operation is not None:
                    _HEALTH["spend_operations_processed"] = (
                        int(_HEALTH["spend_operations_processed"]) + 1
                    )

                _record_tick()
                if publication is not None or spend_operation is not None:
                    await asyncio.sleep(0)
                else:
                    await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                _record_error(exc)
                log.exception("Advertising runtime worker iteration failed")
                await asyncio.sleep(min(self.interval_seconds * 2.0, 30.0))
            except RuntimeError as exc:
                _record_error(exc)
                log.exception("Advertising runtime worker iteration failed")
                await asyncio.sleep(min(self.interval_seconds * 2.0, 30.0))
            except TypeError as exc:
                _record_error(exc)
                log.exception("Advertising runtime worker iteration failed")
                await asyncio.sleep(min(self.interval_seconds * 2.0, 30.0))
            except ValueError as exc:
                _record_error(exc)
                log.exception("Advertising runtime worker iteration failed")
                await asyncio.sleep(min(self.interval_seconds * 2.0, 30.0))


__all__ = [
    "AdPublicationWorker",
    "ad_publication_worker_health_snapshot",
]

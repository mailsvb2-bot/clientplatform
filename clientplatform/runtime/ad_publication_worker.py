from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from clientplatform.application.ad_connections import (
    ad_connections_enabled,
    process_one_ad_publication,
    yandex_direct_provider_configured,
)
from core.task_manager import TaskManager

log = logging.getLogger(__name__)


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


@dataclass(slots=True)
class AdPublicationWorker:
    task_manager: TaskManager
    interval_seconds: float = 2.0
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
        )

    def start(self) -> bool:
        if self._running:
            return False
        if not ad_connections_enabled() or not yandex_direct_provider_configured():
            return False
        self._running = True
        self._task = self.task_manager.create(
            self._run(),
            name="clientplatform-ad-publication-worker",
        )
        return True

    async def stop(self) -> None:
        self._running = False
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
        while self._running:
            try:
                processed = await asyncio.to_thread(process_one_ad_publication)
                if processed is None:
                    await asyncio.sleep(self.interval_seconds)
                else:
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError, ValueError, TypeError):  # validator: allow-wide-except
                log.exception("Advertising publication worker iteration failed")
                await asyncio.sleep(min(self.interval_seconds * 2.0, 30.0))


__all__ = ["AdPublicationWorker"]

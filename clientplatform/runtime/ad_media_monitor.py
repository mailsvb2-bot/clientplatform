from __future__ import annotations

import asyncio
import logging

from core.runtime_env import env_int
from core.task_manager import TaskManager
from clientplatform.application.ad_goal_publication import process_one_pending_video_asset


log = logging.getLogger(__name__)
_task_manager = TaskManager()
_task: asyncio.Task[None] | None = None


def _interval_seconds() -> int:
    return env_int(
        "CLIENTPLATFORM_AD_MEDIA_MONITOR_INTERVAL_SEC",
        60,
        minimum=30,
        maximum=600,
    )


async def _loop() -> None:
    try:
        while True:
            try:
                for _ in range(10):
                    processed = await asyncio.to_thread(process_one_pending_video_asset)
                    if not processed:
                        break
            except Exception:  # validator: allow-wide-except
                # Persistent provider IDs make retry on the next tick safe.
                log.exception("ClientPlatform ad media monitor tick failed")
            await asyncio.sleep(_interval_seconds())
    except asyncio.CancelledError:
        raise


async def start_ad_media_monitor(bot: object) -> None:
    del bot
    global _task
    if _task is not None and not _task.done():
        return
    _task = _task_manager.create(_loop(), name="clientplatform-ad-media-monitor")


async def stop_ad_media_monitor(bot: object) -> None:
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


__all__ = ["start_ad_media_monitor", "stop_ad_media_monitor"]

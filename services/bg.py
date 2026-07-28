from __future__ import annotations

import asyncio
import logging

from core.task_manager import TaskManager

log = logging.getLogger(__name__)

_tm: TaskManager | None = None
_a1_owner_task: asyncio.Task[None] | None = None
_a1_media_gateway_task: asyncio.Task[None] | None = None


def _running_loop_available(owner_name: str) -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        log.warning("%s was not bound: no running event loop", owner_name)
        return False
    return True


def _bind_a1_runtime_owner(task_manager: TaskManager) -> None:
    """Attach the optional A1 dispatch owner to the canonical process lifecycle."""

    global _a1_owner_task

    from a1.runtime.dispatch_runtime import dispatch_runtime_config

    if not dispatch_runtime_config().enabled:
        return
    if not _running_loop_available("A1 runtime owner"):
        return
    if _a1_owner_task is not None and not _a1_owner_task.done():
        return

    from a1.runtime.owner import run_a1_runtime_owner

    _a1_owner_task = task_manager.create(
        run_a1_runtime_owner(),
        name="a1-runtime-owner",
    )


def _bind_a1_media_gateway_owner(task_manager: TaskManager) -> None:
    """Attach the optional media gateway to the same process lifecycle."""

    global _a1_media_gateway_task

    from a1.runtime.media_gateway import media_gateway_config

    if not media_gateway_config().enabled:
        return
    if not _running_loop_available("A1 media gateway owner"):
        return
    if _a1_media_gateway_task is not None and not _a1_media_gateway_task.done():
        return

    from a1.runtime.media_gateway import run_media_gateway_owner

    _a1_media_gateway_task = task_manager.create(
        run_media_gateway_owner(),
        name="a1-media-gateway-owner",
    )


def bind_task_manager(task_manager: TaskManager) -> TaskManager:
    """Bind the process-wide canonical TaskManager.

    Runtime owners such as DB writer, legacy scheduler, A1 dispatch and the
    optional A1 media gateway use the same lifecycle manager. This prevents
    split task ownership and guarantees cancellation during application
    shutdown or a self-heal restart.
    """

    global _tm
    _tm = task_manager
    _bind_a1_runtime_owner(_tm)
    _bind_a1_media_gateway_owner(_tm)
    return _tm


def tm() -> TaskManager:
    global _tm
    if _tm is None:
        _tm = TaskManager()
    return _tm

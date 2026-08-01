from __future__ import annotations

import asyncio
import logging

from core.task_manager import TaskManager

log = logging.getLogger(__name__)

_tm: TaskManager | None = None
_clientplatform_owner_task: asyncio.Task[None] | None = None
_clientplatform_media_gateway_task: asyncio.Task[None] | None = None


def _running_loop_available(owner_name: str) -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        log.warning("%s was not bound: no running event loop", owner_name)
        return False
    return True


def _bind_clientplatform_runtime_owner(task_manager: TaskManager) -> None:
    """Attach the optional clientplatform dispatch owner to the canonical process lifecycle."""

    global _clientplatform_owner_task

    from clientplatform.runtime.dispatch_runtime import dispatch_runtime_config

    if not dispatch_runtime_config().enabled:
        return
    if not _running_loop_available("clientplatform runtime owner"):
        return
    if _clientplatform_owner_task is not None and not _clientplatform_owner_task.done():
        return

    from clientplatform.runtime.owner import run_clientplatform_runtime_owner

    _clientplatform_owner_task = task_manager.create(
        run_clientplatform_runtime_owner(),
        name="clientplatform-runtime-owner",
    )


def _bind_clientplatform_media_gateway_owner(task_manager: TaskManager) -> None:
    """Attach the optional media gateway to the same process lifecycle."""

    global _clientplatform_media_gateway_task

    from clientplatform.runtime.media_gateway import media_gateway_config

    if not media_gateway_config().enabled:
        return
    if not _running_loop_available("clientplatform media gateway owner"):
        return
    if _clientplatform_media_gateway_task is not None and not _clientplatform_media_gateway_task.done():
        return

    from clientplatform.runtime.media_gateway import run_media_gateway_owner

    _clientplatform_media_gateway_task = task_manager.create(
        run_media_gateway_owner(),
        name="clientplatform-media-gateway-owner",
    )


def register_task_manager(task_manager: TaskManager) -> TaskManager:
    """Set the canonical manager without starting optional runtime owners."""

    global _tm
    _tm = task_manager
    return _tm


def bind_task_manager(task_manager: TaskManager) -> TaskManager:
    """Bind optional runtime owners to the process-wide canonical TaskManager.

    DB writer and scheduler call :func:`tm` during startup, so callers register
    the canonical manager before starting those services. The optional
    ClientPlatform owners are then bound after fatal startup steps succeed and
    can be rebound after a dispatcher shutdown cancels their previous tasks.
    """

    canonical = register_task_manager(task_manager)
    _bind_clientplatform_runtime_owner(canonical)
    _bind_clientplatform_media_gateway_owner(canonical)
    return canonical


def tm() -> TaskManager:
    global _tm
    if _tm is None:
        _tm = TaskManager()
    return _tm

from __future__ import annotations

import asyncio
import logging

from core.task_manager import TaskManager

log = logging.getLogger(__name__)

_tm: TaskManager | None = None
_a1_owner_task: asyncio.Task[None] | None = None


def _bind_a1_runtime_owner(task_manager: TaskManager) -> None:
    """Attach the optional A1 dispatch owner to the canonical process lifecycle."""

    global _a1_owner_task

    from a1.runtime.dispatch_runtime import dispatch_runtime_config

    if not dispatch_runtime_config().enabled:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        log.warning("A1 runtime owner was not bound: no running event loop")
        return
    if _a1_owner_task is not None and not _a1_owner_task.done():
        return

    from a1.runtime.owner import run_a1_runtime_owner

    _a1_owner_task = task_manager.create(
        run_a1_runtime_owner(),
        name="a1-runtime-owner",
    )


def bind_task_manager(task_manager: TaskManager) -> TaskManager:
    """Bind the process-wide canonical TaskManager.

    Runtime owners such as DB writer, legacy scheduler and the optional A1
    dispatch runtime use the same lifecycle manager. This prevents split task
    ownership and guarantees that A1 dispatch is cancelled during application
    shutdown or a self-heal restart.
    """

    global _tm
    _tm = task_manager
    _bind_a1_runtime_owner(_tm)
    return _tm


def tm() -> TaskManager:
    global _tm
    if _tm is None:
        _tm = TaskManager()
    return _tm

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

log = logging.getLogger(__name__)

ShutdownCall = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class ShutdownStep:
    name: str
    call: ShutdownCall


async def run_shutdown_steps(steps: Iterable[ShutdownStep]) -> None:
    """Run every shutdown step even when an earlier component fails.

    Ordinary cleanup failures are logged and isolated. Cancellation is delayed
    until all registered steps have had a chance to release their resources,
    then re-raised so the outer runtime still observes cancellation correctly.
    """

    cancellation: asyncio.CancelledError | None = None
    for step in steps:
        try:
            await step.call()
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
            log.warning("Shutdown step cancelled: %s", step.name)
        except Exception:  # validator: allow-wide-except
            log.exception("Shutdown step failed: %s", step.name)

    if cancellation is not None:
        raise cancellation

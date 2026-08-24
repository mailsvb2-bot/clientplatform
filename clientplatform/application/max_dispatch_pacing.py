from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from clientplatform.domain.connections import ConnectionPlatform

# MAX documents two independent limits relevant to canonical outbound delivery:
# no more than two messages per second per dialog and no more than 30 API
# requests per second for stable bot operation. Keep a small safety margin so
# scheduler jitter does not turn the documented ceiling into routine HTTP 429s.
_MAX_DIALOG_MIN_INTERVAL_SECONDS = 0.55
_MAX_CONNECTION_MIN_INTERVAL_SECONDS = 0.04

_pacing_lock = threading.Lock()
_next_connection_write_at: dict[str, float] = {}
_next_dialog_write_at: dict[tuple[str, str], float] = {}


def _reserve_max_provider_slot(item: Any, *, now: float | None = None) -> float:
    """Reserve one MAX provider-write start time and return required delay.

    The reservation is deliberately synchronous and happens before the durable
    non-replay boundary. The canonical dispatch worker is single-owner in normal
    runtime composition; the lock also makes explicit/manual concurrent batch
    invocations conservative rather than able to burst through provider limits.
    """

    dispatch = getattr(item, "dispatch", None)
    if dispatch is None or getattr(dispatch, "platform", None) != ConnectionPlatform.MAX:
        return 0.0

    connection_id = str(getattr(dispatch, "connection_id", "") or "").strip()
    external_subject = str(getattr(item, "external_subject", "") or "").strip()
    if not connection_id or not external_subject:
        return 0.0

    observed = time.monotonic() if now is None else float(now)
    dialog_key = (connection_id, external_subject)
    with _pacing_lock:
        target = max(
            observed,
            _next_connection_write_at.get(connection_id, observed),
            _next_dialog_write_at.get(dialog_key, observed),
        )
        _next_connection_write_at[connection_id] = (
            target + _MAX_CONNECTION_MIN_INTERVAL_SECONDS
        )
        _next_dialog_write_at[dialog_key] = target + _MAX_DIALOG_MIN_INTERVAL_SECONDS

        # Keep long-lived workers bounded without deleting an active reservation.
        if len(_next_dialog_write_at) > 4096:
            expired = [
                key
                for key, value in _next_dialog_write_at.items()
                if value <= observed and key != dialog_key
            ]
            for key in expired[:2048]:
                _next_dialog_write_at.pop(key, None)
        if len(_next_connection_write_at) > 1024:
            expired_connections = [
                key
                for key, value in _next_connection_write_at.items()
                if value <= observed and key != connection_id
            ]
            for key in expired_connections[:512]:
                _next_connection_write_at.pop(key, None)

    return max(0.0, target - observed)


async def pace_max_provider_boundary(item: Any) -> bool:
    """Wait for a safe MAX write slot before any non-replay marker is set.

    Returns True when an actual wait happened. Callers can use that signal to
    revalidate live authorization after the wait and immediately before provider
    I/O. Cancellation is intentionally propagated: at this point the dispatch is
    still replay-safe and its lease can be released normally.
    """

    delay = _reserve_max_provider_slot(item)
    if delay <= 0:
        return False
    await asyncio.sleep(delay)
    return True


__all__ = ["pace_max_provider_boundary"]

from __future__ import annotations

"""Thread-safe storage for short-lived user interaction states.

The state is intentionally in-memory: after a process restart the user simply
starts the small flow again. Reads never consume state. A state is removed only
by ``consume_pending`` after the business input has been validated, or by an
explicit cancellation. This prevents invalid input from silently ending a flow
and lets routers consume only the state they own.
"""

from dataclasses import dataclass
import logging
from threading import RLock
from time import time
from typing import Any, Collection

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Pending:
    user_id: int
    kind: str
    data: dict[str, Any] | None
    created_ts: float
    ttl_sec: int


_PENDING: dict[int, Pending] = {}
_PENDING_LOCK = RLock()


def _user_id(value: int) -> int:
    return int(value)


def _live_pending_locked(user_id: int) -> Pending | None:
    pending = _PENDING.get(user_id)
    if pending is None:
        return None
    if (time() - pending.created_ts) > int(pending.ttl_sec):
        _PENDING.pop(user_id, None)
        return None
    return pending


def _expected_kinds(expected_kinds: str | Collection[str] | None) -> frozenset[str] | None:
    if expected_kinds is None:
        return None
    if isinstance(expected_kinds, str):
        return frozenset({expected_kinds})
    return frozenset(str(kind) for kind in expected_kinds)


def set_pending(
    user_id: int,
    kind: str,
    data: dict[str, Any] | None = None,
    *,
    ttl_sec: int = 600,
) -> None:
    """Replace the user's pending state with one validated state."""

    try:
        uid = _user_id(user_id)
        normalized_kind = str(kind or "").strip()
        ttl = int(ttl_sec)
        if not normalized_kind:
            raise ValueError("pending kind must not be empty")
        if ttl <= 0:
            raise ValueError("pending ttl_sec must be positive")
        pending = Pending(
            user_id=uid,
            kind=normalized_kind,
            data=dict(data or {}),
            created_ts=time(),
            ttl_sec=ttl,
        )
        with _PENDING_LOCK:
            _PENDING[uid] = pending
    except (TypeError, ValueError):
        log.exception("pending.set_pending failed")


def peek_pending(user_id: int) -> Pending | None:
    """Return a live state without consuming it."""

    try:
        uid = _user_id(user_id)
        with _PENDING_LOCK:
            return _live_pending_locked(uid)
    except (TypeError, ValueError):
        log.exception("pending.peek_pending failed")
        return None


def consume_pending(
    user_id: int,
    expected_kinds: str | Collection[str] | None = None,
) -> Pending | None:
    """Atomically remove and return a live state when its kind is expected.

    A mismatching state is left untouched. Handlers call this only after their
    input is valid; cancellation handlers use the kind scope so they cannot
    steal another flow's state.
    """

    try:
        uid = _user_id(user_id)
        expected = _expected_kinds(expected_kinds)
        with _PENDING_LOCK:
            pending = _live_pending_locked(uid)
            if pending is None:
                return None
            if expected is not None and pending.kind not in expected:
                return None
            _PENDING.pop(uid, None)
            return pending
    except (TypeError, ValueError):
        log.exception("pending.consume_pending failed")
        return None


def pop_pending(user_id: int) -> Pending | None:
    """Backward-compatible unconditional consume."""

    return consume_pending(user_id)


def clear_pending(
    user_id: int,
    expected_kinds: str | Collection[str] | None = None,
) -> bool:
    """Clear a pending state, optionally only when its kind matches."""

    return consume_pending(user_id, expected_kinds) is not None

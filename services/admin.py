from __future__ import annotations

"""High-trust platform administration boundary.

Business/team authorization belongs to ClientPlatform tenancy and business-member
roles.  This module intentionally answers only the separate platform-operator
question and therefore has no database-backed shadow role or permission model.
"""

from config.settings import ADMIN_IDS


def _uid(user_id: int | None) -> int | None:
    if user_id is None:
        return None
    try:
        value = int(user_id)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def is_platform_admin(user_id: int | None) -> bool:
    """Return whether ``user_id`` is an explicitly configured platform operator."""

    uid = _uid(user_id)
    if uid is None:
        return False
    return uid in {int(value) for value in (ADMIN_IDS or [])}


__all__ = ["is_platform_admin"]

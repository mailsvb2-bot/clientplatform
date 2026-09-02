from __future__ import annotations

"""Read-only platform-owner control plane.

This module intentionally stays outside tenant/business authorization. Platform
operators may inspect platform health here, but this boundary does not grant access
to any business record, workspace or customer data.
"""

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from services.admin import is_platform_admin
from services.disaster_recovery_status import disaster_recovery_status
from services.platform_resource_limits import get_platform_resource_snapshot
from services.release_contract_report import format_runtime_contract_report


class PlatformOperatorPermissionDenied(PermissionError):
    """Raised when a caller is not an explicitly configured platform operator."""


def platform_operator_snapshot(
    user_id: int | None,
    *,
    include_resource_telemetry: bool = False,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Return one platform-wide, read-only operator snapshot.

    ``include_resource_telemetry`` is opt-in because the canonical Visual Creative
    resource source performs a bounded read from the configured gateway. The default
    snapshot therefore remains local/read-only and performs no provider call.
    """

    if not is_platform_admin(user_id):
        raise PlatformOperatorPermissionDenied("platform operator access required")

    generated_at = now_utc or datetime.now(tz=UTC)
    if generated_at.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")

    recovery = disaster_recovery_status(include_hash=False)
    resource_telemetry: dict[str, Any] = {
        "requested": False,
        "status": "NOT_REQUESTED",
        "snapshot": None,
    }
    if include_resource_telemetry:
        resources = get_platform_resource_snapshot()
        resource_telemetry = {
            "requested": True,
            "status": "AVAILABLE" if resources.telemetry_available else "UNAVAILABLE",
            "snapshot": asdict(resources),
        }

    return {
        "scope": "platform",
        "business_data_included": False,
        "generated_at_utc": generated_at.astimezone(UTC).isoformat(),
        "release_contract": {
            "report": format_runtime_contract_report(),
        },
        "disaster_recovery": recovery.to_dict(),
        "resource_telemetry": resource_telemetry,
    }


__all__ = [
    "PlatformOperatorPermissionDenied",
    "platform_operator_snapshot",
]

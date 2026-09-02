from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from handlers import clientplatform_entry as entry
from services import platform_operator_dashboard as operator


@pytest.mark.asyncio
async def test_platform_status_denies_non_operator_without_snapshot_data() -> None:
    message = SimpleNamespace(
        from_user=SimpleNamespace(
            id=9001,
            username="ordinary-user",
            full_name="Ordinary User",
        ),
        answer=AsyncMock(),
    )
    with (
        patch.object(entry.control, "_user_id", return_value=9001),
        patch.object(
            operator,
            "platform_operator_snapshot",
            side_effect=operator.PlatformOperatorPermissionDenied(
                "platform operator access required"
            ),
        ) as snapshot,
    ):
        await entry.clientplatform_platform_status_command(message)

    snapshot.assert_called_once_with(9001)
    message.answer.assert_awaited_once_with(
        "Доступ к состоянию платформы недоступен."
    )


@pytest.mark.asyncio
async def test_platform_status_returns_read_only_snapshot_to_operator() -> None:
    message = SimpleNamespace(
        from_user=SimpleNamespace(
            id=42,
            username="platform-owner",
            full_name="Platform Owner",
        ),
        answer=AsyncMock(),
    )
    snapshot = {
        "scope": "platform",
        "business_data_included": False,
        "release_contract": {"report": "Runtime contract: GREEN"},
        "disaster_recovery": {"status": "GREEN", "reason": "restore_target_configured"},
        "resource_telemetry": {"status": "NOT_REQUESTED"},
    }
    with (
        patch.object(entry.control, "_user_id", return_value=42),
        patch.object(operator, "platform_operator_snapshot", return_value=snapshot) as load,
    ):
        await entry.clientplatform_platform_status_command(message)

    load.assert_called_once_with(42)
    text = message.answer.await_args.args[0]
    assert "Runtime contract: GREEN" in text
    assert "Disaster recovery: GREEN" in text
    assert "Resource telemetry: NOT_REQUESTED" in text
    assert "business" not in text.casefold()

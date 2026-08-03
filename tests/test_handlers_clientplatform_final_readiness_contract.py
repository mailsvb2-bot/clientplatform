from __future__ import annotations

from types import SimpleNamespace

import pytest

from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.tenancy import TenantAccessDenied
from clientplatform.runtime import admin_observability
from core import telegram_multi_egress
from handlers import clientplatform_admin_extension as extension

# These contracts protect the release gate from test-order-dependent runtime state.


@pytest.mark.asyncio
async def test_optional_tenant_read_is_fail_soft_for_missing_membership() -> None:
    def denied_read() -> None:
        raise TenantAccessDenied("active business membership was not found")

    assert await extension._optional_thread(denied_read, default=[]) == []


@pytest.mark.asyncio
async def test_optional_thread_can_forward_function_default_explicitly() -> None:
    def read_setting(*, default: str) -> str:
        return default

    result = await extension._optional_thread(
        read_setting,
        default="false",
        forward_default=True,
    )
    assert result == "false"


@pytest.mark.asyncio
async def test_offering_snapshot_skips_active_capability_without_id() -> None:
    capabilities = [SimpleNamespace(status=CapabilityStatus.ACTIVE)]
    assert await extension._all_offerings(object(), capabilities) == []


def test_telegram_polling_readiness_is_explicitly_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "CLIENTPLATFORM_REQUIRE_TELEGRAM_POLLING_READY",
        raising=False,
    )
    assert telegram_multi_egress.telegram_readiness_required() is False

    monkeypatch.setenv("CLIENTPLATFORM_REQUIRE_TELEGRAM_POLLING_READY", "1")
    assert telegram_multi_egress.telegram_readiness_required() is True


def test_admin_observability_readiness_is_explicitly_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "CLIENTPLATFORM_REQUIRE_ADMIN_OBSERVABILITY_READY",
        raising=False,
    )
    assert admin_observability._monitor_readiness_required() is False

    monkeypatch.setenv(
        "CLIENTPLATFORM_REQUIRE_ADMIN_OBSERVABILITY_READY",
        "1",
    )
    assert admin_observability._monitor_readiness_required() is True

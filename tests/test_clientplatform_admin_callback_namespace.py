from __future__ import annotations

from types import SimpleNamespace

import pytest

from handlers import (
    _ClientPlatformAdminCallbackNamespace,
    _install_admin_callback_namespace_guard,
)


def _decode_business_token(value: str) -> str:
    if value != "business-token":
        raise ValueError("not a business token")
    return "00000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_admin_callback_guard_does_not_swallow_advertising_namespace() -> None:
    guard = _ClientPlatformAdminCallbackNamespace(_decode_business_token)

    assert await guard(SimpleNamespace(data="cpa:business-token:menu")) is True
    assert await guard(SimpleNamespace(data="cpa:business-token:today")) is True
    assert await guard(SimpleNamespace(data="cpa:formats:business-token")) is True
    assert await guard(SimpleNamespace(data="cpa:back:business-token")) is True

    advertising_callbacks = (
        "cpa:connect:business-token",
        "cpa:home:business-token",
        "cpa:slot:business-token:slot-token",
        "cpa:conn:0",
        "cpa:campaign:0",
        "cpa:disconnects:business-token",
        "cpa:disconnect:business-token:connection-token",
        "cpa:revoke:business-token:connection-token",
        "cpa:yandex-cancel:business-token",
    )
    for callback_data in advertising_callbacks:
        assert await guard(SimpleNamespace(data=callback_data)) is False


class _Observer:
    def __init__(self) -> None:
        self.filters: list[object] = []

    def filter(self, *filters: object) -> None:
        self.filters.extend(filters)


@pytest.mark.asyncio
async def test_admin_callback_guard_is_installed_once_and_accepts_live_admin_token() -> None:
    observer = _Observer()
    admin_module = SimpleNamespace(
        router=SimpleNamespace(callback_query=observer),
        _callback_namespace_guard_composed=False,
    )
    control_module = SimpleNamespace(_token_uuid=_decode_business_token)

    _install_admin_callback_namespace_guard(admin_module, control_module)
    _install_admin_callback_namespace_guard(admin_module, control_module)

    assert admin_module._callback_namespace_guard_composed is True
    assert len(observer.filters) == 1
    guard = observer.filters[0]
    assert await guard(SimpleNamespace(data="cpa:business-token:menu")) is True
    assert await guard(SimpleNamespace(data="cpa:connect:business-token")) is False

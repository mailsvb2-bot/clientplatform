from __future__ import annotations

from collections.abc import Callable
from types import ModuleType

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery


class ClientPlatformAdminCallbackNamespace(BaseFilter):
    """Keep admin ``cpa:`` callbacks from swallowing advertising actions."""

    def __init__(self, decode_token: Callable[[str], str]):
        self._decode_token = decode_token

    async def __call__(self, event: CallbackQuery) -> bool:
        data = str(event.data or "")
        parts = data.split(":")
        if len(parts) < 3 or parts[0] != "cpa":
            return False

        # Current advertising callbacks are action-first (for example
        # ``cpa:connect:<business-token>``). Canonical admin callbacks carry
        # the business token in the second segment instead:
        # ``cpa:<business-token>:<admin-action>``.
        if parts[1] == "home":
            return False
        token = (
            parts[2]
            if parts[1] in {"formats", "back"} and len(parts) == 3
            else parts[1]
        )
        try:
            self._decode_token(token)
        except (TypeError, ValueError):
            return False
        return True


def install_admin_callback_namespace_guard(
    admin_module: ModuleType,
    control_module: ModuleType,
) -> None:
    if bool(getattr(admin_module, "_callback_namespace_guard_composed", False)):
        return
    admin_module.router.callback_query.filter(
        ClientPlatformAdminCallbackNamespace(control_module._token_uuid)
    )
    admin_module._callback_namespace_guard_composed = True


__all__ = [
    "ClientPlatformAdminCallbackNamespace",
    "install_admin_callback_namespace_guard",
]

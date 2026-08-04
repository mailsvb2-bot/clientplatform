from __future__ import annotations

import inspect

from clientplatform.runtime import admin_observability


def test_admin_observability_hooks_accept_aiogram_bot_keyword() -> None:
    """Aiogram injects startup and shutdown workflow data by parameter name."""

    assert list(
        inspect.signature(
            admin_observability.start_admin_observability
        ).parameters
    ) == ["bot"]
    assert list(
        inspect.signature(
            admin_observability.stop_admin_observability
        ).parameters
    ) == ["bot"]

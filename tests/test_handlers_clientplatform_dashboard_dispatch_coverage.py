from __future__ import annotations

from types import SimpleNamespace

from handlers import clientplatform_dashboard_dispatch as dashboard_dispatch


def test_dashboard_dispatch_install_is_idempotent() -> None:
    module = SimpleNamespace(_dynamic_dashboard_dispatch_installed=True)

    dashboard_dispatch.install_dynamic_dashboard_dispatch(module)

    assert module._dynamic_dashboard_dispatch_installed is True

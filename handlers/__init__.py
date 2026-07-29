"""Lazy ClientPlatform handler exports.

Production imports ``clientplatform_control`` from this package. Loading either
public ClientPlatform module first imports the entry router, which performs the
single idempotent router composition after all public modules are initialized.
"""

from __future__ import annotations

import importlib
from types import ModuleType


def _load_clientplatform_modules() -> tuple[ModuleType, ModuleType]:
    entry = importlib.import_module(".clientplatform_entry", __name__)
    control = importlib.import_module(".clientplatform_control", __name__)
    globals()["clientplatform_entry"] = entry
    globals()["clientplatform_control"] = control

    bot_setup = importlib.import_module(".clientplatform_bot_setup", __name__)
    globals()["clientplatform_bot_setup"] = bot_setup
    bot_setup.install_dashboard_button(control)
    if not bool(getattr(entry, "_managed_bot_setup_composed", False)):
        entry.router.include_router(bot_setup.router)
        entry._managed_bot_setup_composed = True
    return entry, control


def __getattr__(name: str) -> ModuleType:
    if name == "clientplatform_control":
        _, control = _load_clientplatform_modules()
        return control
    if name == "clientplatform_entry":
        entry, _ = _load_clientplatform_modules()
        return entry
    if name == "clientplatform_bot_setup":
        _load_clientplatform_modules()
        return globals()["clientplatform_bot_setup"]
    raise AttributeError(name)


__all__ = [
    "clientplatform_bot_setup",
    "clientplatform_control",
    "clientplatform_entry",
]

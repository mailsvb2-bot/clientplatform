"""Lazy handler package composition.

The package stays dependency-light for imports of unrelated handlers. Production
uses ``from handlers import clientplatform_control``; that one attribute composes
the dual-role entry router before the existing ClientPlatform control router.
"""

from __future__ import annotations

import importlib
from types import ModuleType


def _compose_clientplatform_router() -> ModuleType:
    control = importlib.import_module(".clientplatform_control", __name__)
    if not bool(getattr(control, "_dual_role_entry_composed", False)):
        entry = importlib.import_module(".clientplatform_entry", __name__)
        original_router = control.router
        entry.router.include_router(original_router)
        control.router = entry.router
        control._dual_role_entry_composed = True
        globals()["clientplatform_entry"] = entry
    globals()["clientplatform_control"] = control
    return control


def __getattr__(name: str) -> ModuleType:
    if name == "clientplatform_control":
        return _compose_clientplatform_router()
    if name == "clientplatform_entry":
        _compose_clientplatform_router()
        return globals()[name]
    raise AttributeError(name)


__all__ = ["clientplatform_control", "clientplatform_entry"]

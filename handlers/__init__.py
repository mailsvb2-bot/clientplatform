"""Lazy ClientPlatform handler exports.

Production imports ``clientplatform_control`` from this package. Loading either
public ClientPlatform module first imports the entry router, which performs the
single idempotent router composition after all public modules are initialized.
"""

from __future__ import annotations

import importlib
from types import ModuleType

from aiogram import Router


def _load_clientplatform_modules() -> tuple[ModuleType, ModuleType]:
    entry = importlib.import_module(".clientplatform_entry", __name__)
    control = importlib.import_module(".clientplatform_control", __name__)
    globals()["clientplatform_entry"] = entry
    globals()["clientplatform_control"] = control

    program_media = importlib.import_module(
        ".clientplatform_program_media_router",
        __name__,
    )
    globals()["clientplatform_program_media_router"] = program_media
    if not bool(getattr(entry, "_program_media_router_composed", False)):
        original_entry_router = entry.router
        media_entry_router = Router(name="clientplatform_media_entry")
        media_entry_router.include_router(program_media.router)
        media_entry_router.include_router(original_entry_router)
        entry.router = media_entry_router
        control.router = media_entry_router
        entry._program_media_router_composed = True

    if not bool(getattr(entry, "_telegram_commands_startup_composed", False)):
        entry.router.startup.register(entry.register_clientplatform_bot_commands)
        entry._telegram_commands_startup_composed = True

    bot_setup = importlib.import_module(".clientplatform_bot_setup", __name__)
    globals()["clientplatform_bot_setup"] = bot_setup
    bot_setup.install_dashboard_button(control)

    bot_lifecycle = importlib.import_module(
        ".clientplatform_bot_lifecycle",
        __name__,
    )
    globals()["clientplatform_bot_lifecycle"] = bot_lifecycle
    bot_lifecycle.install_lifecycle_controls(bot_setup)
    if not bool(getattr(bot_setup, "_managed_bot_lifecycle_composed", False)):
        bot_setup.router.include_router(bot_lifecycle.router)
        bot_setup._managed_bot_lifecycle_composed = True

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
    if name == "clientplatform_bot_lifecycle":
        _load_clientplatform_modules()
        return globals()["clientplatform_bot_lifecycle"]
    if name == "clientplatform_program_media_router":
        _load_clientplatform_modules()
        return globals()["clientplatform_program_media_router"]
    raise AttributeError(name)


__all__ = [
    "clientplatform_bot_lifecycle",
    "clientplatform_bot_setup",
    "clientplatform_control",
    "clientplatform_entry",
    "clientplatform_program_media_router",
]

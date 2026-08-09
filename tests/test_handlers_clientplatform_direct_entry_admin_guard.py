from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_direct_entry_composition_installs_admin_callback_namespace_guard() -> None:
    code = r'''
import asyncio
import importlib
from types import SimpleNamespace
from uuid import uuid4

entry = importlib.import_module("handlers.clientplatform_entry")
admin = importlib.import_module("handlers.clientplatform_admin")
control = importlib.import_module("handlers.clientplatform_control")
guard_module = importlib.import_module("handlers.clientplatform_admin_callback_guard")

assert control.router is entry.router
assert getattr(admin, "_callback_namespace_guard_composed", False)
filter_ = guard_module.ClientPlatformAdminCallbackNamespace(control._token_uuid)
token = control._uuid_token(str(uuid4()))
assert asyncio.run(filter_(SimpleNamespace(data=f"cpa:connect:{token}"))) is False
assert asyncio.run(filter_(SimpleNamespace(data=f"cpa:{token}:menu"))) is True
'''
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

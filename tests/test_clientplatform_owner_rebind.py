from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _named_call(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    )


def _attribute_call(node: ast.AST, owner: str, name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == owner
        and node.func.attr == name
    )


def test_app_rebinds_clientplatform_owners_on_every_dispatcher_startup() -> None:
    tree = ast.parse(Path("app.py").read_text(encoding="utf-8"))
    create_application = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "create_application"
    )
    startup = next(
        node
        for node in create_application.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_on_startup"
    )
    shutdown = next(
        node
        for node in create_application.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_on_shutdown"
    )

    all_bind_calls = [node for node in ast.walk(create_application) if _named_call(node, "bind_task_manager")]
    startup_bind_calls = [node for node in ast.walk(startup) if _named_call(node, "bind_task_manager")]
    shutdown_refs = [
        node
        for node in ast.walk(shutdown)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "tm"
        and node.attr == "shutdown"
    ]

    assert len(all_bind_calls) == 1
    assert startup_bind_calls == all_bind_calls
    assert len(shutdown_refs) == 1


def test_bind_task_manager_recreates_cancelled_clientplatform_owners(monkeypatch) -> None:
    import services.bg as bg
    from core.task_manager import TaskManager

    dispatch_module = ModuleType("clientplatform.runtime.dispatch_runtime")
    owner_module = ModuleType("clientplatform.runtime.owner")
    gateway_module = ModuleType("clientplatform.runtime.media_gateway")

    dispatch_module.dispatch_runtime_config = lambda: SimpleNamespace(enabled=True)
    gateway_module.media_gateway_config = lambda: SimpleNamespace(enabled=True)

    started: list[str] = []
    stopped: list[str] = []

    async def run_owner(name: str) -> None:
        started.append(name)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(name)

    owner_module.run_clientplatform_runtime_owner = lambda: run_owner("dispatch")
    gateway_module.run_media_gateway_owner = lambda: run_owner("media")

    monkeypatch.setitem(sys.modules, dispatch_module.__name__, dispatch_module)
    monkeypatch.setitem(sys.modules, owner_module.__name__, owner_module)
    monkeypatch.setitem(sys.modules, gateway_module.__name__, gateway_module)
    monkeypatch.setattr(bg, "_tm", None)
    monkeypatch.setattr(bg, "_clientplatform_owner_task", None)
    monkeypatch.setattr(bg, "_clientplatform_media_gateway_task", None)

    async def scenario() -> None:
        task_manager = TaskManager()

        bg.bind_task_manager(task_manager)
        await asyncio.sleep(0)
        first_dispatch = bg._clientplatform_owner_task
        first_media = bg._clientplatform_media_gateway_task

        assert started == ["dispatch", "media"]
        assert first_dispatch is not None
        assert first_media is not None

        await task_manager.shutdown()
        assert first_dispatch.done()
        assert first_media.done()
        assert sorted(stopped) == ["dispatch", "media"]

        bg.bind_task_manager(task_manager)
        await asyncio.sleep(0)
        second_dispatch = bg._clientplatform_owner_task
        second_media = bg._clientplatform_media_gateway_task

        assert started == ["dispatch", "media", "dispatch", "media"]
        assert second_dispatch is not None and second_dispatch is not first_dispatch
        assert second_media is not None and second_media is not first_media

        await task_manager.shutdown()
        assert sorted(stopped) == ["dispatch", "dispatch", "media", "media"]

    asyncio.run(scenario())

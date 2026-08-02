from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Coroutine

import pytest

from services import prewarm


class FakeTask:
    def done(self) -> bool:
        return False


class FakeTaskManager:
    def __init__(self) -> None:
        self.names: list[str | None] = []

    def create(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
    ) -> FakeTask:
        self.names.append(name)
        coro.close()
        return FakeTask()


@pytest.mark.asyncio
async def test_clientplatform_container_defers_optional_prewarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeTaskManager()
    monkeypatch.setenv("CLIENTPLATFORM_DEPLOYMENT_MODE", "container")
    monkeypatch.delenv("CLIENTPLATFORM_DEFER_PREWARM", raising=False)
    monkeypatch.setattr(prewarm, "_audio_prewarm_task", None)
    monkeypatch.setattr(prewarm, "_matplotlib_prewarm_task", None)

    from services import bg

    monkeypatch.setattr(bg, "tm", lambda: manager)

    await prewarm.prewarm_audio_cache(SimpleNamespace())  # type: ignore[arg-type]
    await prewarm.prewarm_matplotlib_cache()

    assert manager.names == [
        "clientplatform-audio-prewarm",
        "clientplatform-matplotlib-prewarm",
    ]


@pytest.mark.asyncio
async def test_non_clientplatform_callers_keep_awaited_prewarm_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.delenv("CLIENTPLATFORM_DEPLOYMENT_MODE", raising=False)
    monkeypatch.delenv("CLIENTPLATFORM_DEFER_PREWARM", raising=False)

    async def audio_worker(_bot: Any) -> None:
        calls.append("audio")

    async def matplotlib_worker() -> None:
        calls.append("matplotlib")

    monkeypatch.setattr(prewarm, "_prewarm_audio_cache_worker", audio_worker)
    monkeypatch.setattr(
        prewarm,
        "_prewarm_matplotlib_cache_worker",
        matplotlib_worker,
    )

    await prewarm.prewarm_audio_cache(SimpleNamespace())  # type: ignore[arg-type]
    await prewarm.prewarm_matplotlib_cache()

    assert calls == ["audio", "matplotlib"]

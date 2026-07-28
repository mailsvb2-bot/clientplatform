from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.runtime import lifecycle, owner
from clientplatform.runtime.dispatch_runtime import DispatchRuntimeConfig
from core.task_manager import TaskManager
from services import bg


def _config(*, enabled: bool) -> DispatchRuntimeConfig:
    return DispatchRuntimeConfig(
        enabled=enabled,
        interval_seconds=5.0,
        tick_timeout_seconds=120.0,
        batch_size=10,
        max_attempts=8,
        lock_ttl_seconds=900,
        http_timeout_seconds=20.0,
    )


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _Connection:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _query):
        return _Rows(self._rows)


class _ConnectionContext:
    def __init__(self, rows):
        self._connection = _Connection(rows)

    def __enter__(self):
        return self._connection

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False


class ClientPlatformSchemaReadinessTests(unittest.TestCase):
    def test_complete_clientplatform_schema_is_required(self) -> None:
        complete = sorted(owner._CLIENTPLATFORM_REQUIRED_TABLES)
        with (
            patch.object(owner, "CONFIG", SimpleNamespace(uses_postgres=False)),
            patch.object(owner, "get_connection", return_value=_ConnectionContext([(name,) for name in complete])),
        ):
            self.assertEqual(owner._clientplatform_schema_readiness(), (True, None))

        with (
            patch.object(owner, "CONFIG", SimpleNamespace(uses_postgres=False)),
            patch.object(owner, "get_connection", return_value=_ConnectionContext([(name,) for name in complete[:-1]])),
        ):
            ready, error = owner._clientplatform_schema_readiness()
        self.assertFalse(ready)
        self.assertEqual(error, f"clientplatform_schema_missing:{complete[-1]}")


class ClientPlatformRuntimeOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        lifecycle._dispatch_scheduler = None

    async def asyncTearDown(self) -> None:
        lifecycle._dispatch_scheduler = None

    async def test_disabled_owner_is_completely_dormant(self) -> None:
        def exploding_probe():
            raise AssertionError("disabled runtime must not inspect the database")

        await owner.run_clientplatform_runtime_owner(
            config=_config(enabled=False),
            schema_probe=exploding_probe,
        )
        self.assertFalse(lifecycle.clientplatform_runtime_health_snapshot()["clientplatform_runtime_composed"])

    async def test_owner_starts_after_schema_and_stops_on_cancellation(self) -> None:
        runtime = object()
        start = AsyncMock(return_value=True)
        stop = AsyncMock(return_value=None)
        task_manager = TaskManager()

        with (
            patch.object(owner, "build_dispatch_runtime", return_value=runtime),
            patch.object(owner, "start_clientplatform_runtime", start),
            patch.object(owner, "stop_clientplatform_runtime", stop),
        ):
            task = task_manager.create(
                owner.run_clientplatform_runtime_owner(
                    config=_config(enabled=True),
                    schema_probe=lambda: (True, None),
                ),
                name="test-clientplatform-runtime-owner",
            )
            for _ in range(20):
                await asyncio.sleep(0)
                if start.await_count:
                    break
            self.assertEqual(start.await_count, 1)
            start.assert_awaited_once_with(runtime)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await task_manager.shutdown()

        stop.assert_awaited_once_with()

    async def test_owner_fails_closed_when_schema_never_becomes_ready(self) -> None:
        monotonic_values = iter((0.0, 2.0))
        with (
            patch.object(owner, "_schema_wait_timeout_seconds", return_value=1.0),
            patch.object(owner, "_schema_poll_interval_seconds", return_value=0.05),
        ):
            with self.assertRaisesRegex(RuntimeError, "clientplatform_runtime_schema_timeout"):
                await owner.run_clientplatform_runtime_owner(
                    config=_config(enabled=True),
                    schema_probe=lambda: (False, "clientplatform_schema_missing:connections"),
                    sleep=AsyncMock(return_value=None),
                    monotonic=lambda: next(monotonic_values),
                )

    async def test_disabled_scheduler_does_not_claim_lifecycle_composition(self) -> None:
        runtime = SimpleNamespace(config=SimpleNamespace(enabled=False))
        self.assertFalse(await lifecycle.start_clientplatform_runtime(runtime))
        snapshot = lifecycle.clientplatform_runtime_health_snapshot()
        self.assertFalse(snapshot["clientplatform_runtime_composed"])
        self.assertFalse(snapshot["clientplatform_dispatch_enabled"])

    async def test_configured_stopped_gateway_makes_runtime_health_unavailable(self) -> None:
        gateway = {
            "clientplatform_media_gateway_configured": True,
            "clientplatform_media_gateway_health_available": True,
            "clientplatform_media_gateway_running": False,
        }
        with patch(
            "clientplatform.runtime.media_gateway.media_gateway_health_snapshot",
            return_value=gateway,
        ):
            snapshot = lifecycle.clientplatform_runtime_health_snapshot()
        self.assertFalse(snapshot["clientplatform_runtime_health_available"])
        self.assertTrue(snapshot["clientplatform_media_gateway_configured"])
        self.assertFalse(snapshot["clientplatform_media_gateway_running"])


class CanonicalTaskManagerBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._previous_tm = bg._tm
        self._previous_owner_task = bg._clientplatform_owner_task
        self._previous_gateway_task = bg._clientplatform_media_gateway_task
        bg._tm = None
        bg._clientplatform_owner_task = None
        bg._clientplatform_media_gateway_task = None

    async def asyncTearDown(self) -> None:
        tasks = [bg._clientplatform_owner_task, bg._clientplatform_media_gateway_task]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in tasks if task is not None),
            return_exceptions=True,
        )
        bg._tm = self._previous_tm
        bg._clientplatform_owner_task = self._previous_owner_task
        bg._clientplatform_media_gateway_task = self._previous_gateway_task

    async def test_binding_owns_clientplatform_runtime_with_the_same_task_manager(self) -> None:
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def fake_owner() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        task_manager = TaskManager()
        with (
            patch(
                "clientplatform.runtime.dispatch_runtime.dispatch_runtime_config",
                return_value=SimpleNamespace(enabled=True),
            ),
            patch(
                "clientplatform.runtime.media_gateway.media_gateway_config",
                return_value=SimpleNamespace(enabled=False),
            ),
            patch("clientplatform.runtime.owner.run_clientplatform_runtime_owner", new=fake_owner),
        ):
            self.assertIs(bg.bind_task_manager(task_manager), task_manager)
            await asyncio.wait_for(started.wait(), timeout=1.0)
            self.assertIsNotNone(bg._clientplatform_owner_task)
            self.assertIn(bg._clientplatform_owner_task, task_manager.tasks)
            await task_manager.shutdown()
            await asyncio.wait_for(stopped.wait(), timeout=1.0)

    async def test_binding_owns_media_gateway_with_the_same_task_manager(self) -> None:
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def fake_gateway_owner() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        task_manager = TaskManager()
        with (
            patch(
                "clientplatform.runtime.dispatch_runtime.dispatch_runtime_config",
                return_value=SimpleNamespace(enabled=False),
            ),
            patch(
                "clientplatform.runtime.media_gateway.media_gateway_config",
                return_value=SimpleNamespace(enabled=True),
            ),
            patch(
                "clientplatform.runtime.media_gateway.run_media_gateway_owner",
                new=fake_gateway_owner,
            ),
        ):
            self.assertIs(bg.bind_task_manager(task_manager), task_manager)
            await asyncio.wait_for(started.wait(), timeout=1.0)
            self.assertIsNotNone(bg._clientplatform_media_gateway_task)
            self.assertIn(bg._clientplatform_media_gateway_task, task_manager.tasks)
            await task_manager.shutdown()
            await asyncio.wait_for(stopped.wait(), timeout=1.0)


if __name__ == "__main__":
    unittest.main()

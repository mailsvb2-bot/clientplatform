from __future__ import annotations

import asyncio
import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.task_manager import TaskManager


_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None


@unittest.skipUnless(_AIOHTTP_AVAILABLE, "aiohttp runtime dependency is not installed")
class MaxWebhookReconciliationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_stop_cancels_reconciliation_before_runner_cleanup(self) -> None:
        from runtime.messenger_webhooks import MessengerWebhookRuntime

        manager = TaskManager()
        entered = asyncio.Event()

        async def _daemon() -> None:
            entered.set()
            await asyncio.Event().wait()

        task = manager.create(_daemon(), name="max-reconcile-test")
        await entered.wait()
        runner = SimpleNamespace(cleanup=AsyncMock())
        runtime = MessengerWebhookRuntime(
            runner=runner,  # type: ignore[arg-type]
            site=SimpleNamespace(),  # type: ignore[arg-type]
            max_webhook_reconciliation_task=task,
        )

        await runtime.stop()

        self.assertTrue(task.done())
        self.assertTrue(task.cancelled())
        runner.cleanup.assert_awaited_once()

    async def test_reconciliation_loop_uses_bounded_canonical_batches(self) -> None:
        from clientplatform.runtime.native_messenger_reconciliation import (
            MaxWebhookReconcileResult,
        )
        from runtime import messenger_webhooks

        reconcile = AsyncMock(
            return_value=MaxWebhookReconcileResult(
                scanned=1,
                reconciled=1,
                failed=0,
                next_cursor=None,
            )
        )
        sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])

        def _env_float(name: str, default: float, **_kwargs: object) -> float:
            if name.endswith("INITIAL_DELAY_SEC"):
                return 5.0
            return default

        with (
            patch.object(messenger_webhooks, "reconcile_max_webhook_batch", reconcile),
            patch.object(messenger_webhooks, "env_float", side_effect=_env_float),
            patch.object(messenger_webhooks, "env_int", return_value=37),
            patch.object(
                messenger_webhooks,
                "_messenger_public_base_url",
                return_value="https://client.example.test",
            ),
            patch.object(messenger_webhooks.asyncio, "sleep", sleep),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await messenger_webhooks._run_max_webhook_reconciliation_loop()

        reconcile.assert_awaited_once_with(
            public_base_url="https://client.example.test",
            cursor=None,
            limit=37,
            request_delay_seconds=0.05,
        )
        self.assertEqual(sleep.await_count, 2)

    async def test_health_fails_closed_when_reconciliation_task_has_died(self) -> None:
        from runtime import messenger_webhooks

        async def _finished() -> None:
            return None

        manager = TaskManager()
        task = manager.create(_finished(), name="max-reconcile-finished")
        await task
        request = SimpleNamespace(
            app={"clientplatform_max_webhook_reconciliation_task": task}
        )

        response = await messenger_webhooks._health(request)  # type: ignore[arg-type]

        self.assertEqual(response.status, 200)
        self.assertIn(b'"ok": false', response.body)
        self.assertIn(b'"max_webhook_reconciliation": false', response.body)


if __name__ == "__main__":
    unittest.main()

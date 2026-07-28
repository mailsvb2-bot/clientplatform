from __future__ import annotations

import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from runtime import health_server


def _snapshot(**overrides):
    value = {
        'a1_dispatch_configured': False,
        'a1_runtime_health_available': True,
        'a1_runtime_composed': False,
        'a1_dispatch_enabled': False,
        'a1_dispatch_running': False,
        'a1_dispatch_iterations': 0,
        'a1_dispatch_claimed': 0,
        'a1_dispatch_sent': 0,
        'a1_dispatch_retried': 0,
        'a1_dispatch_dead': 0,
        'a1_dispatch_errors': 0,
        'a1_dispatch_last_error': '',
        'a1_dispatch_last_tick_age_seconds': 0,
    }
    value.update(overrides)
    return value


class A1DispatchReadinessTests(unittest.TestCase):
    def test_disabled_a1_runtime_is_neutral_for_legacy_readiness(self) -> None:
        ready, errors, flags = health_server._a1_dispatch_readiness(_snapshot())

        self.assertTrue(ready)
        self.assertEqual(errors, [])
        self.assertTrue(flags['a1_dispatch_ready'])
        self.assertFalse(flags['a1_dispatch_degraded'])

    def test_enabled_runtime_fails_closed_when_health_is_unavailable(self) -> None:
        ready, errors, flags = health_server._a1_dispatch_readiness(
            _snapshot(
                a1_dispatch_configured=True,
                a1_runtime_health_available=False,
            )
        )

        self.assertFalse(ready)
        self.assertEqual(errors, ['a1_dispatch:health_unavailable'])
        self.assertTrue(flags['a1_dispatch_degraded'])

    def test_enabled_runtime_requires_composed_owner(self) -> None:
        ready, errors, _flags = health_server._a1_dispatch_readiness(
            _snapshot(a1_dispatch_configured=True)
        )

        self.assertFalse(ready)
        self.assertEqual(errors, ['a1_dispatch:not_composed'])

    def test_running_runtime_without_current_error_is_ready(self) -> None:
        ready, errors, flags = health_server._a1_dispatch_readiness(
            _snapshot(
                a1_dispatch_configured=True,
                a1_runtime_composed=True,
                a1_dispatch_enabled=True,
                a1_dispatch_running=True,
                a1_dispatch_iterations=3,
                a1_dispatch_last_tick_age_seconds=2,
            )
        )

        self.assertTrue(ready)
        self.assertEqual(errors, [])
        self.assertFalse(flags['a1_dispatch_recent_error'])
        self.assertFalse(flags['a1_dispatch_stale'])

    def test_current_tick_error_makes_readiness_fail(self) -> None:
        ready, errors, flags = health_server._a1_dispatch_readiness(
            _snapshot(
                a1_dispatch_configured=True,
                a1_runtime_composed=True,
                a1_dispatch_enabled=True,
                a1_dispatch_running=True,
                a1_dispatch_iterations=4,
                a1_dispatch_errors=1,
                a1_dispatch_last_error='dispatch_tick_timeout',
            )
        )

        self.assertFalse(ready)
        self.assertIn('a1_dispatch:recent_tick_error', errors)
        self.assertTrue(flags['a1_dispatch_recent_error'])

    def test_stale_successful_tick_makes_readiness_fail(self) -> None:
        with patch.dict(
            os.environ,
            {'A1_DISPATCH_READY_MAX_LAST_TICK_AGE_SEC': '10'},
            clear=False,
        ):
            ready, errors, flags = health_server._a1_dispatch_readiness(
                _snapshot(
                    a1_dispatch_configured=True,
                    a1_runtime_composed=True,
                    a1_dispatch_enabled=True,
                    a1_dispatch_running=True,
                    a1_dispatch_iterations=1,
                    a1_dispatch_last_tick_age_seconds=11,
                )
            )

        self.assertFalse(ready)
        self.assertIn('a1_dispatch:stale_tick', errors)
        self.assertTrue(flags['a1_dispatch_stale'])


class A1HealthPayloadIntegrationTests(unittest.TestCase):
    def _patch_common_payload_dependencies(self, stack: ExitStack) -> None:
        stack.enter_context(
            patch.object(
                health_server,
                'CONFIG',
                SimpleNamespace(engine='sqlite', uses_postgres=False),
            )
        )
        stack.enter_context(patch.object(health_server, 'redacted_db_target', return_value='redacted'))
        stack.enter_context(patch.object(health_server, '_telegram_transport', return_value='polling'))
        stack.enter_context(
            patch.object(health_server, '_messenger_webhook_configured', return_value=False)
        )
        stack.enter_context(patch.object(health_server, '_webhook_configured', return_value=False))
        stack.enter_context(
            patch.object(
                health_server,
                '_messenger_preflight_readiness',
                return_value=(True, [], {}),
            )
        )
        stack.enter_context(patch.object(health_server, '_ingress_health_fields', return_value={}))
        stack.enter_context(patch.object(health_server, '_storage_health_fields', return_value={}))
        stack.enter_context(patch.object(health_server, 'ai_policy_snapshot', return_value={}))

    def test_health_payload_exposes_a1_runtime_diagnostics(self) -> None:
        a1 = _snapshot(
            a1_dispatch_configured=True,
            a1_runtime_composed=True,
            a1_dispatch_enabled=True,
            a1_dispatch_running=True,
            a1_dispatch_iterations=2,
            a1_dispatch_sent=7,
        )
        with ExitStack() as stack:
            self._patch_common_payload_dependencies(stack)
            stack.enter_context(patch.object(health_server, '_scheduler_snapshot', return_value={}))
            stack.enter_context(patch.object(health_server, '_a1_runtime_snapshot', return_value=a1))
            payload, status = health_server.build_health_payload()

        self.assertEqual(status, 200)
        self.assertTrue(payload['a1_dispatch_configured'])
        self.assertTrue(payload['a1_dispatch_running'])
        self.assertEqual(payload['a1_dispatch_sent'], 7)

    def test_readiness_fails_when_enabled_a1_scheduler_is_not_running(self) -> None:
        a1 = _snapshot(
            a1_dispatch_configured=True,
            a1_runtime_composed=True,
            a1_dispatch_enabled=True,
            a1_dispatch_running=False,
        )
        with ExitStack() as stack:
            self._patch_common_payload_dependencies(stack)
            stack.enter_context(patch.object(health_server, '_db_ready', return_value=(True, None)))
            stack.enter_context(patch.object(health_server, '_schema_ready', return_value=(True, None)))
            stack.enter_context(patch.object(health_server, '_scheduler_snapshot', return_value={}))
            stack.enter_context(
                patch.object(
                    health_server,
                    '_scheduler_readiness',
                    return_value=(True, [], {}),
                )
            )
            stack.enter_context(patch.object(health_server, '_a1_runtime_snapshot', return_value=a1))
            stack.enter_context(patch.object(health_server, '_audio_ready', return_value=(True, None)))
            stack.enter_context(patch.object(health_server, 'http_ingress_enabled', return_value=False))
            stack.enter_context(patch.object(health_server, 'required_readiness_tables', return_value=[]))
            payload, status = health_server.build_readiness_payload()

        self.assertEqual(status, 500)
        self.assertFalse(payload['ok'])
        self.assertFalse(payload['a1_dispatch_ready'])
        self.assertIn('a1_dispatch:not_running', payload['error'])


if __name__ == '__main__':
    unittest.main()

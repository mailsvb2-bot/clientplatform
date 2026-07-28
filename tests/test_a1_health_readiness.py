from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from a1.runtime import health


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
        ready, errors, flags = health.a1_dispatch_readiness(_snapshot())

        self.assertTrue(ready)
        self.assertEqual(errors, [])
        self.assertTrue(flags['a1_dispatch_ready'])
        self.assertFalse(flags['a1_dispatch_degraded'])

    def test_enabled_runtime_fails_closed_when_health_is_unavailable(self) -> None:
        ready, errors, flags = health.a1_dispatch_readiness(
            _snapshot(
                a1_dispatch_configured=True,
                a1_runtime_health_available=False,
            )
        )

        self.assertFalse(ready)
        self.assertEqual(errors, ['a1_dispatch:health_unavailable'])
        self.assertTrue(flags['a1_dispatch_degraded'])

    def test_enabled_runtime_requires_composed_owner(self) -> None:
        ready, errors, _flags = health.a1_dispatch_readiness(
            _snapshot(a1_dispatch_configured=True)
        )

        self.assertFalse(ready)
        self.assertEqual(errors, ['a1_dispatch:not_composed'])

    def test_running_runtime_without_current_error_is_ready(self) -> None:
        ready, errors, flags = health.a1_dispatch_readiness(
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
        ready, errors, flags = health.a1_dispatch_readiness(
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
            ready, errors, flags = health.a1_dispatch_readiness(
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


if __name__ == '__main__':
    unittest.main()

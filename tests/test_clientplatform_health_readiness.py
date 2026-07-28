from __future__ import annotations

import os
import sqlite3
import unittest
from unittest.mock import Mock, patch

from clientplatform.runtime import health


def _snapshot(**overrides):
    value = {
        'clientplatform_dispatch_configured': False,
        'clientplatform_runtime_health_available': True,
        'clientplatform_runtime_composed': False,
        'clientplatform_dispatch_enabled': False,
        'clientplatform_dispatch_running': False,
        'clientplatform_dispatch_iterations': 0,
        'clientplatform_dispatch_claimed': 0,
        'clientplatform_dispatch_sent': 0,
        'clientplatform_dispatch_retried': 0,
        'clientplatform_dispatch_dead': 0,
        'clientplatform_dispatch_errors': 0,
        'clientplatform_dispatch_last_error': '',
        'clientplatform_dispatch_last_tick_age_seconds': 0,
        'clientplatform_dispatch_outbox_checked': True,
        'clientplatform_dispatch_outbox_available': True,
        'clientplatform_dispatch_outbox_due': 0,
        'clientplatform_dispatch_outbox_stale_sending': 0,
        'clientplatform_dispatch_outbox_recent_dead': 0,
        'clientplatform_dispatch_outbox_oldest_due_age_seconds': 0,
    }
    value.update(overrides)
    return value


class ClientPlatformDispatchConfigurationTests(unittest.TestCase):
    def test_dispatch_is_configured_by_default_with_explicit_opt_out(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(health.clientplatform_dispatch_configured())
        with patch.dict(
            os.environ,
            {'CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED': '0'},
            clear=True,
        ):
            self.assertFalse(health.clientplatform_dispatch_configured())
        with patch.dict(
            os.environ,
            {'CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED': 'invalid'},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, 'enabled_invalid'):
                health.clientplatform_dispatch_configured()


class ClientPlatformDispatchReadinessTests(unittest.TestCase):
    def test_disabled_clientplatform_runtime_is_neutral_for_legacy_readiness(self) -> None:
        ready, errors, flags = health.clientplatform_dispatch_readiness(_snapshot())

        self.assertTrue(ready)
        self.assertEqual(errors, [])
        self.assertTrue(flags['clientplatform_dispatch_ready'])
        self.assertFalse(flags['clientplatform_dispatch_runtime_degraded'])
        self.assertFalse(flags['clientplatform_dispatch_outbox_degraded'])

    def test_enabled_runtime_fails_closed_when_health_is_unavailable(self) -> None:
        ready, errors, flags = health.clientplatform_dispatch_readiness(
            _snapshot(
                clientplatform_dispatch_configured=True,
                clientplatform_runtime_health_available=False,
            )
        )

        self.assertFalse(ready)
        self.assertEqual(errors, ['clientplatform_dispatch:health_unavailable'])
        self.assertTrue(flags['clientplatform_dispatch_runtime_degraded'])
        self.assertFalse(flags['clientplatform_dispatch_outbox_degraded'])

    def test_enabled_runtime_requires_composed_owner(self) -> None:
        ready, errors, _flags = health.clientplatform_dispatch_readiness(
            _snapshot(clientplatform_dispatch_configured=True)
        )

        self.assertFalse(ready)
        self.assertEqual(errors, ['clientplatform_dispatch:not_composed'])

    def test_running_runtime_without_current_error_is_ready(self) -> None:
        ready, errors, flags = health.clientplatform_dispatch_readiness(
            _snapshot(
                clientplatform_dispatch_configured=True,
                clientplatform_runtime_composed=True,
                clientplatform_dispatch_enabled=True,
                clientplatform_dispatch_running=True,
                clientplatform_dispatch_iterations=3,
                clientplatform_dispatch_last_tick_age_seconds=2,
            )
        )

        self.assertTrue(ready)
        self.assertEqual(errors, [])
        self.assertFalse(flags['clientplatform_dispatch_recent_error'])
        self.assertFalse(flags['clientplatform_dispatch_stale'])

    def test_current_tick_error_makes_readiness_fail(self) -> None:
        ready, errors, flags = health.clientplatform_dispatch_readiness(
            _snapshot(
                clientplatform_dispatch_configured=True,
                clientplatform_runtime_composed=True,
                clientplatform_dispatch_enabled=True,
                clientplatform_dispatch_running=True,
                clientplatform_dispatch_iterations=4,
                clientplatform_dispatch_errors=1,
                clientplatform_dispatch_last_error='dispatch_tick_timeout',
            )
        )

        self.assertFalse(ready)
        self.assertIn('clientplatform_dispatch:recent_tick_error', errors)
        self.assertTrue(flags['clientplatform_dispatch_recent_error'])

    def test_stale_successful_tick_makes_readiness_fail(self) -> None:
        with patch.dict(
            os.environ,
            {'CLIENTPLATFORM_DISPATCH_READY_MAX_LAST_TICK_AGE_SEC': '10'},
            clear=False,
        ):
            ready, errors, flags = health.clientplatform_dispatch_readiness(
                _snapshot(
                    clientplatform_dispatch_configured=True,
                    clientplatform_runtime_composed=True,
                    clientplatform_dispatch_enabled=True,
                    clientplatform_dispatch_running=True,
                    clientplatform_dispatch_iterations=1,
                    clientplatform_dispatch_last_tick_age_seconds=11,
                )
            )

        self.assertFalse(ready)
        self.assertIn('clientplatform_dispatch:stale_tick', errors)
        self.assertTrue(flags['clientplatform_dispatch_stale'])


class ClientPlatformOutboxSnapshotTests(unittest.TestCase):
    def test_disabled_runtime_does_not_query_outbox(self) -> None:
        probe = Mock(side_effect=AssertionError('disabled clientplatform must not query outbox'))

        snapshot = health.clientplatform_outbox_snapshot(configured=False, probe=probe)

        probe.assert_not_called()
        self.assertFalse(snapshot['clientplatform_dispatch_outbox_checked'])
        self.assertFalse(snapshot['clientplatform_dispatch_outbox_available'])

    def test_enabled_runtime_exposes_aggregate_probe(self) -> None:
        probe = Mock(
            return_value={
                'clientplatform_dispatch_outbox_available': True,
                'clientplatform_dispatch_outbox_due': 7,
                'clientplatform_dispatch_outbox_recent_dead': 2,
            }
        )

        snapshot = health.clientplatform_outbox_snapshot(configured=True, probe=probe)

        self.assertTrue(snapshot['clientplatform_dispatch_outbox_checked'])
        self.assertTrue(snapshot['clientplatform_dispatch_outbox_available'])
        self.assertEqual(snapshot['clientplatform_dispatch_outbox_due'], 7)
        probe.assert_called_once_with(
            stale_lock_seconds=900,
            dead_window_seconds=900,
        )

    def test_database_error_is_redacted_to_error_type(self) -> None:
        def failing_probe(**_kwargs):
            raise sqlite3.OperationalError('secret database details')

        snapshot = health.clientplatform_outbox_snapshot(configured=True, probe=failing_probe)

        self.assertTrue(snapshot['clientplatform_dispatch_outbox_checked'])
        self.assertFalse(snapshot['clientplatform_dispatch_outbox_available'])
        self.assertEqual(snapshot['clientplatform_dispatch_outbox_error'], 'OperationalError')
        self.assertNotIn('secret', str(snapshot))


class ClientPlatformOutboxReadinessTests(unittest.TestCase):
    def _healthy_runtime(self, **overrides):
        return _snapshot(
            clientplatform_dispatch_configured=True,
            clientplatform_runtime_composed=True,
            clientplatform_dispatch_enabled=True,
            clientplatform_dispatch_running=True,
            clientplatform_dispatch_iterations=1,
            **overrides,
        )

    def test_unavailable_enabled_outbox_fails_closed(self) -> None:
        ready, errors, flags = health.clientplatform_dispatch_readiness(
            self._healthy_runtime(clientplatform_dispatch_outbox_available=False)
        )

        self.assertFalse(ready)
        self.assertIn('clientplatform_dispatch_outbox:unavailable', errors)
        self.assertTrue(flags['clientplatform_dispatch_outbox_degraded'])

    def test_due_backlog_and_oldest_due_are_bounded(self) -> None:
        with patch.dict(
            os.environ,
            {
                'CLIENTPLATFORM_DISPATCH_READY_MAX_DUE': '5',
                'CLIENTPLATFORM_DISPATCH_READY_MAX_OLDEST_DUE_AGE_SEC': '60',
            },
            clear=False,
        ):
            ready, errors, flags = health.clientplatform_dispatch_readiness(
                self._healthy_runtime(
                    clientplatform_dispatch_outbox_due=6,
                    clientplatform_dispatch_outbox_oldest_due_age_seconds=61,
                )
            )

        self.assertFalse(ready)
        self.assertIn('clientplatform_dispatch_outbox:due_backlog', errors)
        self.assertIn('clientplatform_dispatch_outbox:oldest_due', errors)
        self.assertTrue(flags['clientplatform_dispatch_outbox_due_backlog'])
        self.assertTrue(flags['clientplatform_dispatch_outbox_oldest_due'])

    def test_stale_leases_and_recent_dead_burst_are_bounded(self) -> None:
        with patch.dict(
            os.environ,
            {
                'CLIENTPLATFORM_DISPATCH_READY_MAX_STALE_SENDING': '0',
                'CLIENTPLATFORM_DISPATCH_READY_MAX_RECENT_DEAD': '2',
            },
            clear=False,
        ):
            ready, errors, flags = health.clientplatform_dispatch_readiness(
                self._healthy_runtime(
                    clientplatform_dispatch_outbox_stale_sending=1,
                    clientplatform_dispatch_outbox_recent_dead=3,
                )
            )

        self.assertFalse(ready)
        self.assertIn('clientplatform_dispatch_outbox:stale_sending', errors)
        self.assertIn('clientplatform_dispatch_outbox:recent_dead', errors)
        self.assertTrue(flags['clientplatform_dispatch_outbox_stale_leases'])
        self.assertTrue(flags['clientplatform_dispatch_outbox_recent_dead_exceeded'])


if __name__ == '__main__':
    unittest.main()

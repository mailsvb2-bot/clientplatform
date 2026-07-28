from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from a1.infrastructure import dispatch_observability


class _Cursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row):
        self._row = row
        self.query = ''
        self.params = ()

    def execute(self, query, params):
        self.query = str(query)
        self.params = tuple(params)
        return _Cursor(self._row)


class _ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False


class DispatchOutboxObservabilityTests(unittest.TestCase):
    def test_aggregate_snapshot_is_global_count_only_and_time_bounded(self) -> None:
        row = {
            'pending_count': 2,
            'retry_count': 3,
            'sending_count': 1,
            'sent_count': 10,
            'dead_count': 4,
            'cancelled_count': 1,
            'due_count': 5,
            'stale_sending_count': 1,
            'recent_dead_count': 2,
            'oldest_due_at': '2026-07-28T09:58:00+00:00',
        }
        connection = _Connection(row)
        now = datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc)

        with patch.object(
            dispatch_observability,
            'get_connection',
            return_value=_ConnectionContext(connection),
        ):
            snapshot = dispatch_observability.load_dispatch_outbox_snapshot(
                now=now,
                stale_lock_seconds=60,
                dead_window_seconds=300,
            )

        self.assertTrue(snapshot['a1_dispatch_outbox_available'])
        self.assertEqual(snapshot['a1_dispatch_outbox_pending'], 2)
        self.assertEqual(snapshot['a1_dispatch_outbox_retry'], 3)
        self.assertEqual(snapshot['a1_dispatch_outbox_due'], 5)
        self.assertEqual(snapshot['a1_dispatch_outbox_stale_sending'], 1)
        self.assertEqual(snapshot['a1_dispatch_outbox_recent_dead'], 2)
        self.assertEqual(snapshot['a1_dispatch_outbox_oldest_due_age_seconds'], 120)
        self.assertNotIn('business_id', connection.query)
        self.assertNotIn('payload_ref', connection.query)
        self.assertEqual(
            connection.params,
            (
                '2026-07-28T10:00:00+00:00',
                '2026-07-28T09:59:00+00:00',
                '2026-07-28T09:55:00+00:00',
                '2026-07-28T10:00:00+00:00',
            ),
        )

    def test_invalid_oldest_timestamp_does_not_break_health(self) -> None:
        row = (0, 0, 0, 0, 0, 0, 0, 0, 0, 'not-a-timestamp')
        connection = _Connection(row)

        with patch.object(
            dispatch_observability,
            'get_connection',
            return_value=_ConnectionContext(connection),
        ):
            snapshot = dispatch_observability.load_dispatch_outbox_snapshot(
                now=datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc)
            )

        self.assertEqual(snapshot['a1_dispatch_outbox_oldest_due_age_seconds'], 0)

    def test_missing_aggregate_row_fails_closed(self) -> None:
        connection = _Connection(None)

        with patch.object(
            dispatch_observability,
            'get_connection',
            return_value=_ConnectionContext(connection),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                'a1_dispatch_outbox_aggregate_unavailable',
            ):
                dispatch_observability.load_dispatch_outbox_snapshot()


if __name__ == '__main__':
    unittest.main()

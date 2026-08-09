from __future__ import annotations

import unittest
from unittest import mock

from services.db import core


class _FakeCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.rowcount = 1
        self.description = None

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self.calls.append((sql, tuple(params)))

    def executemany(self, sql: str, params: object) -> None:
        self.calls.append((sql, tuple(params)))

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self) -> None:
        return None


class _FakeRawConnection:
    def __init__(self) -> None:
        self.cursors: list[_FakeCursor] = []
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        cursor = _FakeCursor()
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _FakePsycopg:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.connections: list[_FakeRawConnection] = []

    def connect(self, _dsn: str, **kwargs: object) -> _FakeRawConnection:
        self.calls.append(dict(kwargs))
        connection = _FakeRawConnection()
        self.connections.append(connection)
        return connection


class ClientPlatformDatabaseDeadlineTests(unittest.TestCase):
    def test_deadline_forces_dedicated_postgres_connection_with_shorter_server_limits(self) -> None:
        driver = _FakePsycopg()
        with (
            mock.patch.object(core, "is_postgres_enabled", return_value=True),
            mock.patch.object(core, "_load_psycopg", return_value=(driver, object())),
            mock.patch.object(core, "DATABASE_URL", "postgresql://example.invalid/db"),
            mock.patch.dict("os.environ", {"POSTGRES_REUSE_CONNECTIONS": "1"}, clear=False),
            core.db_operation_deadline(2.0),
        ):
            connection = core.get_connection()

        self.assertIsInstance(connection, core.PostgresCompatConnection)
        self.assertFalse(connection._reusable)
        self.assertIsNotNone(connection.operation_deadline)
        self.assertEqual(len(driver.calls), 1)
        options = str(driver.calls[0]["options"])
        self.assertIn("statement_timeout=", options)
        self.assertIn("lock_timeout=", options)
        self.assertLessEqual(int(driver.calls[0]["connect_timeout"]), 2)
        connection.close()

    def test_each_statement_recomputes_remaining_absolute_deadline(self) -> None:
        raw = _FakeRawConnection()
        connection = core.PostgresCompatConnection(
            raw,
            operation_deadline=100.0,
        )

        with mock.patch.object(core.time, "monotonic", return_value=99.25):
            connection.execute("SELECT 42")

        calls = raw.cursors[0].calls
        self.assertEqual(calls[0][0], "SELECT set_config('statement_timeout', %s, true)")
        self.assertEqual(calls[0][1], ("750ms",))
        self.assertEqual(calls[1][0], "SELECT set_config('lock_timeout', %s, true)")
        self.assertEqual(calls[2], ("SELECT 42", ()))

    def test_expired_deadline_fails_before_user_statement(self) -> None:
        raw = _FakeRawConnection()
        connection = core.PostgresCompatConnection(
            raw,
            operation_deadline=10.0,
        )

        with (
            mock.patch.object(core.time, "monotonic", return_value=10.01),
            self.assertRaises(core.DatabaseOperationDeadlineExceeded),
        ):
            connection.execute("SELECT 42")

        self.assertEqual(raw.cursors[0].calls, [])

    def test_nested_deadline_can_only_shorten_outer_budget(self) -> None:
        with mock.patch.object(core.time, "monotonic", side_effect=[100.0, 101.0]):
            with core.db_operation_deadline(10.0):
                outer = core._DB_OPERATION_DEADLINE.get()
                with core.db_operation_deadline(30.0):
                    inner = core._DB_OPERATION_DEADLINE.get()

        self.assertEqual(outer, 110.0)
        self.assertEqual(inner, 110.0)


if __name__ == "__main__":
    unittest.main()

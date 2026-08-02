from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from clientplatform.infrastructure.postgres_safe_activity_repository import ActivityRepository


class _Cursor:
    def fetchall(self) -> list[Any]:
        return []


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Cursor:
        self.calls.append((sql, tuple(params)))
        return _Cursor()


class PostgresSafeActivityRepositoryTests(unittest.TestCase):
    def _repository(self) -> tuple[ActivityRepository, _Connection, Any]:
        conn = _Connection()
        actor = SimpleNamespace(business_id="business-1")
        repository = ActivityRepository.__new__(ActivityRepository)
        repository._conn = conn
        repository._current_actor = lambda _actor: actor
        return repository, conn, actor

    def test_list_capabilities_never_places_smallint_directly_in_or(self) -> None:
        repository, conn, actor = self._repository()

        self.assertEqual(repository.list_capabilities(actor=actor), [])

        sql, params = conn.calls[-1]
        self.assertIn("(? = 1 OR status='active')", sql)
        self.assertNotIn("(? OR status='active')", sql)
        self.assertEqual(params, ("business-1", 0))

    def test_list_capabilities_include_disabled_uses_portable_flag(self) -> None:
        repository, conn, actor = self._repository()

        repository.list_capabilities(actor=actor, include_disabled=True)

        _sql, params = conn.calls[-1]
        self.assertEqual(params, ("business-1", 1))

    def test_list_offerings_uses_same_portable_filter(self) -> None:
        repository, conn, actor = self._repository()
        repository.get_capability = lambda **_kwargs: SimpleNamespace(id="capability-1")

        self.assertEqual(
            repository.list_offerings(
                actor=actor,
                capability_id="capability-1",
                include_archived=False,
            ),
            [],
        )

        sql, params = conn.calls[-1]
        self.assertIn("(? = 1 OR status='active')", sql)
        self.assertNotIn("(? OR status='active')", sql)
        self.assertEqual(params, ("business-1", "capability-1", 0))

    def test_application_uses_postgres_safe_repository(self) -> None:
        from clientplatform.application import activity

        self.assertIs(activity.ActivityRepository, ActivityRepository)


if __name__ == "__main__":
    unittest.main()

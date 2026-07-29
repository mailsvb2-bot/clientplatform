from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from services import mood_text_flow


class OptionalMoodSchemaTests(unittest.TestCase):
    def test_missing_postgres_mood_table_means_no_pending_session(self) -> None:
        missing = sqlite3.OperationalError(
            'relation "mood_sessions" does not exist'
        )
        with patch.object(
            mood_text_flow._core,
            "find_pending_pre_session_id",
            side_effect=missing,
        ):
            self.assertIsNone(mood_text_flow.find_pending_pre_session_id(101))

    def test_missing_sqlite_mood_table_means_no_pending_session(self) -> None:
        missing = sqlite3.OperationalError("no such table: mood_sessions")
        with patch.object(
            mood_text_flow._core,
            "find_pending_post_session_id",
            side_effect=missing,
        ):
            self.assertIsNone(mood_text_flow.find_pending_post_session_id(202))

    def test_unrelated_database_error_is_not_hidden(self) -> None:
        failure = sqlite3.OperationalError("database is locked")
        with patch.object(
            mood_text_flow._core,
            "find_pending_pre_session_id",
            side_effect=failure,
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                mood_text_flow.find_pending_pre_session_id(303)


if __name__ == "__main__":
    unittest.main()

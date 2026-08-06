from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from clientplatform.application import ad_oauth_sessions
from clientplatform.domain.ad_connections import AdConnectionInvariantViolation


class BrokenDatabaseContext:
    def __enter__(self):
        raise sqlite3.OperationalError("private database detail")

    def __exit__(self, exc_type, exc, traceback):
        return False


class AdOAuthCancellationErrorTests(unittest.TestCase):
    def test_database_failure_is_mapped_to_sanitized_domain_error(self) -> None:
        actor = type("Actor", (), {"user_id": 101, "business_id": "business-id"})()
        with (
            patch.object(
                ad_oauth_sessions,
                "get_db",
                return_value=BrokenDatabaseContext(),
            ),
            self.assertRaisesRegex(
                AdConnectionInvariantViolation,
                "could not be persisted",
            ) as raised,
        ):
            ad_oauth_sessions.cancel_yandex_direct_oauth(
                actor=actor,
                state="s" * 43,
            )
        self.assertNotIn("private database detail", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

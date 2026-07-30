from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application import program_media as application
from clientplatform.infrastructure import program_media_cleanup_repository as repository_module
from clientplatform.infrastructure.program_media_cleanup import (
    delete_program_media_reference,
)
from clientplatform.infrastructure.program_media_cleanup_repository import (
    ProgramMediaCleanupRepository,
)
from clientplatform.infrastructure.program_media_store import ProgramMediaStoreError


REFERENCE_A = "s3://clientplatform-production/program-media/a/audio/aa/object-a.mp3"
REFERENCE_B = "s3://clientplatform-production/program-media/b/audio/bb/object-b.mp3"


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE lessons(
            id TEXT PRIMARY KEY,
            content_ref TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE program_media_cleanup_queue(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            media_reference TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            locked_at TEXT,
            lock_token TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            dead_at TEXT
        )
        """
    )
    return conn


class FakeResponse:
    def __init__(self, status: int = 204) -> None:
        self.status = status
        self.headers: dict[str, str] = {}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return b""


def _enabled_env() -> dict[str, str]:
    return {
        "CLIENTPLATFORM_PROGRAM_MEDIA_INGEST_ENABLED": "1",
        "CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT": "https://s3.example.test",
        "CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION": "test-1",
        "CLIENTPLATFORM_STORAGE_BUCKET": "clientplatform-production",
        "CLIENTPLATFORM_SECRET_S3_ACCESS_KEY": "access-key",
        "CLIENTPLATFORM_SECRET_S3_SECRET_KEY": "secret-key",
        "CLIENTPLATFORM_PROGRAM_MEDIA_MAX_BYTES": "20000000",
        "CLIENTPLATFORM_PROGRAM_MEDIA_TIMEOUT_SEC": "30",
    }


class ProgramMediaDeleteTests(unittest.TestCase):
    def test_delete_is_signed_bounded_and_idempotent(self) -> None:
        requests: list[Any] = []

        def opener(request: Any, *, timeout: float) -> FakeResponse:
            self.assertEqual(timeout, 30.0)
            requests.append(request)
            return FakeResponse(204)

        self.assertTrue(
            delete_program_media_reference(
                REFERENCE_A,
                env=_enabled_env(),
                opener=opener,
                clock=lambda: datetime(2026, 7, 30, tzinfo=timezone.utc),
            )
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get_method(), "DELETE")
        headers = {name.lower(): value for name, value in requests[0].header_items()}
        self.assertIn("authorization", headers)
        self.assertIn("x-amz-content-sha256", headers)
        self.assertNotIn("secret-key", requests[0].full_url)

    def test_delete_rejects_foreign_or_non_program_objects(self) -> None:
        for reference in (
            "s3://foreign-bucket/program-media/object.mp3",
            "s3://clientplatform-production/backups/object.dump",
            "https://client.example/object.mp3",
        ):
            with self.subTest(reference=reference):
                with self.assertRaisesRegex(
                    ProgramMediaStoreError,
                    "cleanup_reference_invalid",
                ):
                    delete_program_media_reference(
                        reference,
                        env=_enabled_env(),
                        opener=lambda *_args, **_kwargs: FakeResponse(),
                    )


class ProgramMediaCleanupRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _database()
        self.business_id = str(uuid4())
        self.config_patch = patch.object(
            repository_module,
            "is_postgres_enabled",
            return_value=False,
        )
        self.config_patch.start()

    def tearDown(self) -> None:
        self.config_patch.stop()
        self.conn.close()

    def test_grace_period_idempotency_claim_retry_and_complete(self) -> None:
        repository = ProgramMediaCleanupRepository(self.conn)
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        first = repository.enqueue(
            business_id=self.business_id,
            media_reference=REFERENCE_A,
            reason="pending mutation",
            delay_seconds=600,
            now=now,
        )
        second = repository.enqueue(
            business_id=self.business_id,
            media_reference=REFERENCE_A,
            reason="duplicate",
            delay_seconds=600,
            now=now,
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(repository.claim_due(now=now), [])

        claimed = repository.claim_due(now=now + timedelta(seconds=601))
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].status, "processing")
        retried = repository.reschedule(
            claimed[0],
            error="temporary failure",
            max_attempts=3,
            now=now + timedelta(seconds=602),
        )
        self.assertEqual(retried.status, "retry")
        self.assertEqual(retried.attempts, 1)

        claimed_again = repository.claim_due(now=now + timedelta(seconds=700))
        self.assertEqual(len(claimed_again), 1)
        self.assertTrue(repository.complete(claimed_again[0]))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM program_media_cleanup_queue"
            ).fetchone()[0],
            0,
        )


class ProgramMediaCleanupBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _database()
        self.business_id = str(uuid4())
        self.config_patch = patch.object(
            repository_module,
            "is_postgres_enabled",
            return_value=False,
        )
        self.config_patch.start()

    def tearDown(self) -> None:
        self.config_patch.stop()
        self.conn.close()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        yield self.conn
        self.conn.commit()

    def test_batch_retains_referenced_media_and_deletes_only_orphans(self) -> None:
        repository = ProgramMediaCleanupRepository(self.conn)
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        repository.enqueue(
            business_id=self.business_id,
            media_reference=REFERENCE_A,
            reason="old reference",
            now=now,
        )
        repository.enqueue(
            business_id=self.business_id,
            media_reference=REFERENCE_B,
            reason="failed mutation",
            now=now,
        )
        self.conn.execute(
            "INSERT INTO lessons(id,content_ref) VALUES(?,?)",
            (str(uuid4()), REFERENCE_A),
        )
        self.conn.commit()
        deleted: list[str] = []

        with (
            patch.object(application, "get_db", self._db),
            patch.object(application, "_cleanup_enabled", return_value=True),
            patch.object(
                application,
                "delete_program_media_reference",
                side_effect=lambda reference: deleted.append(reference) or True,
            ),
        ):
            result = application.run_program_media_cleanup_batch(limit=10)

        self.assertEqual(result.claimed, 2)
        self.assertEqual(result.retained, 1)
        self.assertEqual(result.deleted, 1)
        self.assertEqual(deleted, [REFERENCE_B])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM program_media_cleanup_queue"
            ).fetchone()[0],
            0,
        )

    def test_batch_reschedules_and_eventually_marks_terminal_failure(self) -> None:
        ProgramMediaCleanupRepository(self.conn).enqueue(
            business_id=self.business_id,
            media_reference=REFERENCE_B,
            reason="failed mutation",
            now=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
        )
        self.conn.commit()
        with (
            patch.object(application, "get_db", self._db),
            patch.object(application, "_cleanup_enabled", return_value=True),
            patch.object(
                application,
                "delete_program_media_reference",
                side_effect=ProgramMediaStoreError(
                    "program_media_cleanup_transport_failure",
                    retryable=True,
                ),
            ),
        ):
            result = application.run_program_media_cleanup_batch(
                limit=10,
                max_attempts=1,
            )

        self.assertEqual(result.claimed, 1)
        self.assertEqual(result.dead, 1)
        row = self.conn.execute(
            "SELECT status,attempts,last_error FROM program_media_cleanup_queue"
        ).fetchone()
        self.assertEqual(tuple(row), ("dead", 1, "program_media_cleanup_transport_failure"))


if __name__ == "__main__":
    unittest.main()

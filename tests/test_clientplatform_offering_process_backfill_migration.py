from __future__ import annotations

import json
import sqlite3
import unittest

from clientplatform.domain.offering_process import default_ai_profile, default_process_mechanics
from clientplatform.infrastructure.offering_process_repository import OfferingProcessRepository
from services.db import schema as db_schema
from services.migrations import apply_all_migrations
from services.migrations import clientplatform_offering_process_backfill_v1 as migration


_STAMP = "2026-09-15T10:00:00+00:00"
_BACKFILL_STAMP = "2026-09-15T12:00:00+00:00"
_BUSINESS_ID = "00000000-0000-0000-0000-000000000001"
_MEMBER_ID = "00000000-0000-0000-0000-000000000002"
_CAPABILITY_ID = "00000000-0000-0000-0000-000000000003"
_ACTIVE_ID = "00000000-0000-0000-0000-000000000004"
_ARCHIVED_ID = "00000000-0000-0000-0000-000000000005"
_CONFIGURED_ID = "00000000-0000-0000-0000-000000000006"


class ClientPlatformOfferingProcessBackfillMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        db_schema.create_or_update_tables(self.conn)
        self.conn.execute(
            """
            INSERT INTO businesses(
                id, name, status, created_by_user_id, created_at, updated_at
            ) VALUES(?, 'Legacy business', 'active', 910001, ?, ?)
            """,
            (_BUSINESS_ID, _STAMP, _STAMP),
        )
        self.conn.execute(
            """
            INSERT INTO business_members(
                id, business_id, user_id, role, status, created_at, updated_at
            ) VALUES(?, ?, 910001, 'owner', 'active', ?, ?)
            """,
            (_MEMBER_ID, _BUSINESS_ID, _STAMP, _STAMP),
        )
        self.conn.execute(
            """
            INSERT INTO business_capabilities(
                id, business_id, connector_key, kind, title, status,
                created_by_member_id, created_at, updated_at
            ) VALUES(?, ?, 'services', 'services', 'Услуги', 'active', ?, ?, ?)
            """,
            (_CAPABILITY_ID, _BUSINESS_ID, _MEMBER_ID, _STAMP, _STAMP),
        )
        self._insert_offering(_ACTIVE_ID, status="active")
        self._insert_offering(_ARCHIVED_ID, status="archived")
        self._insert_offering(_CONFIGURED_ID, status="active")
        configured = OfferingProcessRepository(self.conn).ensure(
            business_id=_BUSINESS_ID,
            offering_id=_CONFIGURED_ID,
            created_by_member_id=_MEMBER_ID,
            now=_STAMP,
        )
        self.conn.execute(
            """
            UPDATE business_offering_processes
            SET mechanics_json=?, ai_profile_json=?, revision=revision+1
            WHERE offering_id=? AND business_id=?
            """,
            (
                json.dumps({"schema_version": 9, "sentinel": "keep-me"}, sort_keys=True),
                json.dumps({"schema_version": 9, "sentinel": "keep-ai"}, sort_keys=True),
                configured.offering_id,
                configured.business_id,
            ),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _insert_offering(self, offering_id: str, *, status: str) -> None:
        archived_at = _STAMP if status == "archived" else None
        self.conn.execute(
            """
            INSERT INTO business_offerings(
                id, business_id, capability_id, title, description, status,
                created_by_member_id, created_at, updated_at, archived_at
            ) VALUES(?, ?, ?, ?, 'Legacy offering', ?, ?, ?, ?, ?)
            """,
            (
                offering_id,
                _BUSINESS_ID,
                _CAPABILITY_ID,
                f"Offering {offering_id[-1]}",
                status,
                _MEMBER_ID,
                _STAMP,
                _STAMP,
                archived_at,
            ),
        )

    def test_apply_backfills_only_missing_active_offerings_and_is_idempotent(self) -> None:
        report = migration.reconcile_legacy_active_offering_processes(
            self.conn,
            now=_BACKFILL_STAMP,
        )
        replay = migration.reconcile_legacy_active_offering_processes(
            self.conn,
            now="2026-09-15T13:00:00+00:00",
        )

        self.assertEqual((1, 1), (report.candidates_scanned, report.processes_ensured))
        self.assertEqual((0, 0), (replay.candidates_scanned, replay.processes_ensured))
        active = OfferingProcessRepository(self.conn).get(
            business_id=_BUSINESS_ID,
            offering_id=_ACTIVE_ID,
        )
        self.assertEqual(default_process_mechanics(), active.mechanics)
        self.assertEqual(default_ai_profile(), active.ai_profile)
        self.assertEqual(_BACKFILL_STAMP, active.created_at)
        self.assertIsNone(
            self.conn.execute(
                "SELECT offering_id FROM business_offering_processes WHERE offering_id=?",
                (_ARCHIVED_ID,),
            ).fetchone()
        )
        configured = OfferingProcessRepository(self.conn).get(
            business_id=_BUSINESS_ID,
            offering_id=_CONFIGURED_ID,
        )
        self.assertEqual(2, configured.revision)
        self.assertEqual("keep-me", configured.mechanics["sentinel"])
        self.assertEqual("keep-ai", configured.ai_profile["sentinel"])

    def test_canonical_migration_pipeline_backfills_legacy_active_offering(self) -> None:
        with self.conn:
            apply_all_migrations(self.conn)

        active = OfferingProcessRepository(self.conn).get(
            business_id=_BUSINESS_ID,
            offering_id=_ACTIVE_ID,
        )
        self.assertEqual(default_process_mechanics(), active.mechanics)
        self.assertEqual(default_ai_profile(), active.ai_profile)
        self.assertIsNone(
            self.conn.execute(
                "SELECT offering_id FROM business_offering_processes WHERE offering_id=?",
                (_ARCHIVED_ID,),
            ).fetchone()
        )
        self.assertEqual(
            1,
            self.conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
                (migration.NAME,),
            ).fetchone()[0],
        )

    def test_apply_marks_once_and_leaves_archived_legacy_for_explicit_restore(self) -> None:
        with self.conn:
            migration.apply(self.conn)
        with self.conn:
            migration.apply(self.conn)

        self.assertEqual(
            1,
            self.conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
                (migration.NAME,),
            ).fetchone()[0],
        )
        self.assertEqual(
            2,
            self.conn.execute(
                "SELECT COUNT(*) FROM business_offering_processes"
            ).fetchone()[0],
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT offering_id FROM business_offering_processes WHERE offering_id=?",
                (_ARCHIVED_ID,),
            ).fetchone()
        )


if __name__ == "__main__":
    unittest.main()

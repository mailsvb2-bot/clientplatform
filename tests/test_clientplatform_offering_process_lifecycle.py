from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import services.db.core as db_core
from clientplatform.application.activity import (
    archive_business_offering,
    create_business_offering,
    enable_business_capability,
    get_business_offering_process,
    list_business_offerings,
    rename_business_offering,
    restore_business_offering,
    run_offering_process_retention_batch,
    save_business_profile,
)
from clientplatform.application.tenancy import (
    create_business,
    resolve_tenant_context,
)
from clientplatform.domain.activity import ActivityInvariantViolation, ActivityNotFound
from clientplatform.domain.offering_process import (
    OfferingProcessState,
    default_ai_profile,
    default_process_mechanics,
)
from clientplatform.privacy_manifest import validate_clientplatform_privacy_manifest
from services.db import get_db, get_db_ro
from services.schema import init_db


class ClientPlatformOfferingProcessLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_db_path = db_core.DB_PATH
        self._tmpdir = tempfile.TemporaryDirectory(prefix="clientplatform-offering-process-")
        db_core.DB_PATH = Path(self._tmpdir.name) / "offering-process.db"
        init_db()

        access_a = create_business(owner_user_id=910001, name="Практика")
        access_b = create_business(owner_user_id=910002, name="Чужой бизнес")
        self.owner_a = resolve_tenant_context(
            user_id=910001,
            business_id=access_a.business.id,
        )
        self.owner_b = resolve_tenant_context(
            user_id=910002,
            business_id=access_b.business.id,
        )
        save_business_profile(
            actor=self.owner_a,
            activity_description="Психологические консультации",
            timezone_name="Europe/Moscow",
        )
        self.capability = enable_business_capability(
            actor=self.owner_a,
            connector_key="consultations",
        )
        self.offering = create_business_offering(
            actor=self.owner_a,
            capability_id=self.capability.id,
            title="Консультация",
            description="Индивидуальная встреча",
            idempotency_key="offering-process-base",
            now="2026-09-15T08:00:00+00:00",
        )

    def tearDown(self) -> None:
        db_core.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    def _seed_generated_process(self) -> None:
        with get_db() as conn:
            conn.execute(
                """
                UPDATE business_offering_processes
                SET mechanics_json=?, ai_profile_json=?, revision=revision+1,
                    updated_at='2026-09-15T09:00:00+00:00'
                WHERE offering_id=? AND business_id=?
                """,
                (
                    json.dumps(
                        {
                            "schema_version": 7,
                            "generated_funnel": "KEEP-ME-WITHIN-RETENTION",
                        },
                        sort_keys=True,
                    ),
                    json.dumps(
                        {
                            "schema_version": 7,
                            "ad_copy": {"prompt_ref": "COPY-SENTINEL"},
                            "followup_copy": {"prompt_ref": "FOLLOWUP-SENTINEL"},
                            "image_generation": {"prompt_ref": "IMAGE-SENTINEL"},
                        },
                        sort_keys=True,
                    ),
                    self.offering.id,
                    self.owner_a.business_id,
                ),
            )

    def test_process_is_one_to_one_and_rename_preserves_generated_mechanics(self) -> None:
        self._seed_generated_process()
        before = get_business_offering_process(
            actor=self.owner_a,
            offering_id=self.offering.id,
        )

        renamed = rename_business_offering(
            actor=self.owner_a,
            offering_id=self.offering.id,
            title="Разбор ситуации 60 минут",
            now="2026-09-15T10:00:00+00:00",
        )
        after = get_business_offering_process(
            actor=self.owner_a,
            offering_id=self.offering.id,
        )

        self.assertEqual(self.offering.id, renamed.id)
        self.assertEqual("Разбор ситуации 60 минут", renamed.title)
        self.assertEqual(before.offering_id, after.offering_id)
        self.assertEqual(before.mechanics, after.mechanics)
        self.assertEqual(before.ai_profile, after.ai_profile)
        self.assertNotIn("Консультация", after.mechanics_json)
        self.assertNotIn("Разбор ситуации", after.mechanics_json)
        with get_db_ro() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM clientplatform_admin_audit_events "
                "WHERE business_id=? AND subject_id=? AND action='offering_renamed'",
                (self.owner_a.business_id, self.offering.id),
            ).fetchone()["c"]
        self.assertEqual(1, count)

    def test_archive_freezes_for_seven_days_and_restore_reuses_exact_process(self) -> None:
        self._seed_generated_process()
        archived = archive_business_offering(
            actor=self.owner_a,
            offering_id=self.offering.id,
            now="2026-09-15T12:00:00+00:00",
        )
        frozen = get_business_offering_process(
            actor=self.owner_a,
            offering_id=self.offering.id,
        )
        self.assertEqual("archived", archived.status.value)
        self.assertEqual(OfferingProcessState.FROZEN, frozen.state)
        self.assertEqual("2026-09-22T12:00:00+00:00", frozen.purge_after)
        self.assertEqual(
            [],
            list_business_offerings(actor=self.owner_a, capability_id=self.capability.id),
        )
        self.assertEqual(
            [self.offering.id],
            [
                item.id
                for item in list_business_offerings(
                    actor=self.owner_a,
                    capability_id=self.capability.id,
                    include_archived=True,
                )
            ],
        )

        restored = restore_business_offering(
            actor=self.owner_a,
            offering_id=self.offering.id,
            now="2026-09-22T11:59:59+00:00",
        )
        active = get_business_offering_process(
            actor=self.owner_a,
            offering_id=self.offering.id,
        )
        self.assertEqual("active", restored.status.value)
        self.assertEqual(OfferingProcessState.ACTIVE, active.state)
        self.assertEqual("KEEP-ME-WITHIN-RETENTION", active.mechanics["generated_funnel"])
        self.assertEqual("COPY-SENTINEL", active.ai_profile["ad_copy"]["prompt_ref"])
        self.assertIsNone(active.frozen_at)
        self.assertIsNone(active.purge_after)

    def test_due_cleanup_purges_only_rebuildable_process_and_late_restore_rebuilds(self) -> None:
        self._seed_generated_process()
        archive_business_offering(
            actor=self.owner_a,
            offering_id=self.offering.id,
            now="2026-09-15T12:00:00+00:00",
        )
        self.assertEqual(
            1,
            run_offering_process_retention_batch(
                now="2026-09-22T12:00:00+00:00",
                limit=10,
            ),
        )
        purged = get_business_offering_process(
            actor=self.owner_a,
            offering_id=self.offering.id,
        )
        self.assertEqual(OfferingProcessState.PURGED, purged.state)
        self.assertEqual({}, purged.mechanics)
        self.assertEqual({}, purged.ai_profile)

        restored = restore_business_offering(
            actor=self.owner_a,
            offering_id=self.offering.id,
            now="2026-09-23T12:00:00+00:00",
        )
        rebuilt = get_business_offering_process(
            actor=self.owner_a,
            offering_id=self.offering.id,
        )
        self.assertEqual(self.offering.id, restored.id)
        self.assertEqual(OfferingProcessState.ACTIVE, rebuilt.state)
        self.assertEqual(default_process_mechanics(), rebuilt.mechanics)
        self.assertEqual(default_ai_profile(), rebuilt.ai_profile)
        self.assertNotIn("SENTINEL", rebuilt.mechanics_json)
        self.assertNotIn("SENTINEL", rebuilt.ai_profile_json)

    def test_restore_at_deadline_rebuilds_even_before_maintenance_tick(self) -> None:
        self._seed_generated_process()
        archive_business_offering(
            actor=self.owner_a,
            offering_id=self.offering.id,
            now="2026-09-15T12:00:00+00:00",
        )
        restore_business_offering(
            actor=self.owner_a,
            offering_id=self.offering.id,
            now="2026-09-22T12:00:00+00:00",
        )
        rebuilt = get_business_offering_process(
            actor=self.owner_a,
            offering_id=self.offering.id,
        )
        self.assertEqual(default_process_mechanics(), rebuilt.mechanics)
        self.assertEqual(default_ai_profile(), rebuilt.ai_profile)

    def test_retention_cleanup_cannot_purge_a_restored_service(self) -> None:
        self._seed_generated_process()
        archive_business_offering(
            actor=self.owner_a,
            offering_id=self.offering.id,
            now="2026-09-15T12:00:00+00:00",
        )
        restore_business_offering(
            actor=self.owner_a,
            offering_id=self.offering.id,
            now="2026-09-20T12:00:00+00:00",
        )
        self.assertEqual(
            0,
            run_offering_process_retention_batch(
                now="2026-09-23T12:00:00+00:00",
                limit=10,
            ),
        )
        active = get_business_offering_process(
            actor=self.owner_a,
            offering_id=self.offering.id,
        )
        self.assertEqual(OfferingProcessState.ACTIVE, active.state)
        self.assertEqual("KEEP-ME-WITHIN-RETENTION", active.mechanics["generated_funnel"])

    def test_cross_tenant_and_archived_rename_fail_closed(self) -> None:
        with self.assertRaises(ActivityNotFound):
            rename_business_offering(
                actor=self.owner_b,
                offering_id=self.offering.id,
                title="Чужое имя",
            )
        archive_business_offering(
            actor=self.owner_a,
            offering_id=self.offering.id,
            now="2026-09-15T12:00:00+00:00",
        )
        with self.assertRaises(ActivityInvariantViolation):
            rename_business_offering(
                actor=self.owner_a,
                offering_id=self.offering.id,
                title="Сначала надо вернуть",
            )
        with self.assertRaises(ActivityNotFound):
            restore_business_offering(
                actor=self.owner_b,
                offering_id=self.offering.id,
            )

    def test_privacy_manifest_knows_rebuildable_process_table(self) -> None:
        with get_db() as conn:
            report = validate_clientplatform_privacy_manifest(
                conn,
                strict=True,
                require_complete=True,
            )
        self.assertTrue(report.ok)
        self.assertIn("business_offering_processes", report.discovered_business_tables)

    def test_retention_batch_rejects_unbounded_limit(self) -> None:
        with self.assertRaises(ValueError):
            run_offering_process_retention_batch(limit=0)
        with self.assertRaises(ValueError):
            run_offering_process_retention_batch(limit=1001)


if __name__ == "__main__":
    unittest.main()

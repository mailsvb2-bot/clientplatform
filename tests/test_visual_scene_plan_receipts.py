from __future__ import annotations

import sqlite3
import unittest

from clientplatform.application.visual_scene_plan_receipts import visual_scene_plan_key
from clientplatform.domain.visual_scene_plan import VisualScenePlanStatus
from clientplatform.domain.visual_style_intent import VisualStyleIntent
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.privacy_manifest import TENANT_POLICIES, validate_clientplatform_privacy_manifest
from clientplatform.infrastructure.visual_scene_plan_receipt_repository import (
    VisualScenePlanReceiptRepository,
)
from services.db.schema import clientplatform_creative_experiments, clientplatform_tenancy


class VisualScenePlanReceiptRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_creative_experiments.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Scene planning")
        self.actor = tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.repo = VisualScenePlanReceiptRepository(self.conn)
        self.request = "девушка смотрит зимой в окно"
        self.style = VisualStyleIntent(realism="photorealistic")
        self.plan_key, self.normalized_request, self.style_json = visual_scene_plan_key(
            request=self.request,
            style_intent=self.style,
        )

    def tearDown(self) -> None:
        self.conn.close()

    def claim(self):
        return self.repo.claim(
            actor=self.actor,
            plan_key=self.plan_key,
            request_text=self.normalized_request,
            style_json=self.style_json,
            now="2026-10-04T07:00:00+00:00",
        )

    def test_same_plan_claim_is_durable_and_idempotent(self) -> None:
        first, first_created = self.claim()
        second, second_created = self.claim()

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.plan_key, first.plan_key)
        self.assertEqual(second.status, VisualScenePlanStatus.PLANNING)

    def test_completed_plan_is_reused_without_second_external_call_claim(self) -> None:
        receipt, created = self.claim()
        self.assertTrue(created)
        ready = self.repo.complete(
            actor=self.actor,
            receipt_id=receipt.id,
            result_json='{"version":1,"scene_contract":{},"planner_source":"deterministic","variants":[]}',
            now="2026-10-04T07:01:00+00:00",
        )
        self.assertEqual(ready.status, VisualScenePlanStatus.READY)

        repeated, repeated_created = self.claim()
        self.assertFalse(repeated_created)
        self.assertEqual(repeated.status, VisualScenePlanStatus.READY)
        self.assertEqual(repeated.result_json, ready.result_json)

    def test_ambiguous_plan_never_reclaims_automatic_provider_egress(self) -> None:
        receipt, created = self.claim()
        self.assertTrue(created)
        ambiguous = self.repo.mark_ambiguous(
            actor=self.actor,
            receipt_id=receipt.id,
            now="2026-10-04T07:01:00+00:00",
        )
        self.assertEqual(ambiguous.status, VisualScenePlanStatus.AMBIGUOUS)

        repeated, repeated_created = self.claim()
        self.assertFalse(repeated_created)
        self.assertEqual(repeated.id, receipt.id)
        self.assertEqual(repeated.status, VisualScenePlanStatus.AMBIGUOUS)

    def test_scene_plan_table_is_registered_as_erasable_tenant_data(self) -> None:
        report = validate_clientplatform_privacy_manifest(self.conn, strict=True)
        self.assertTrue(report.ok)
        self.assertIn("visual_scene_plan_receipts", TENANT_POLICIES)
        self.assertEqual(
            TENANT_POLICIES["visual_scene_plan_receipts"].disposition,
            "erase",
        )

    def test_plan_key_changes_when_style_changes_but_is_stable_for_same_input(self) -> None:
        first, request, style_json = visual_scene_plan_key(
            request="  девушка   смотрит зимой в окно  ",
            style_intent=self.style,
        )
        second, request_again, style_again = visual_scene_plan_key(
            request="девушка смотрит зимой в окно",
            style_intent=self.style,
        )
        changed, _request, _style = visual_scene_plan_key(
            request=self.request,
            style_intent=VisualStyleIntent(realism="illustrative"),
        )

        self.assertEqual(first, second)
        self.assertEqual(request, request_again)
        self.assertEqual(style_json, style_again)
        self.assertNotEqual(first, changed)


if __name__ == "__main__":
    unittest.main()

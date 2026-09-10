from __future__ import annotations

import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.application import visual_creatives
from clientplatform.application.visual_creatives import (
    create_business_image_from_frozen_payload,
    freeze_business_image_payload,
)
from clientplatform.domain.creative_generation import CreativeGenerationReceiptStatus
from clientplatform.infrastructure.creative_generation_receipt_repository import (
    CreativeGenerationReceiptRepository,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import clientplatform_creative_experiments, clientplatform_tenancy


class FrozenBusinessImagePayloadTests(unittest.TestCase):
    def test_frozen_payload_is_canonical_versioned_and_deterministic(self) -> None:
        first = freeze_business_image_payload(
            request="calm office",
            brand_context="Tone: calm",
            country_code="RU",
        )
        second = freeze_business_image_payload(
            request="calm office",
            brand_context="Tone: calm",
            country_code="RU",
        )
        self.assertEqual(first, second)
        value = json.loads(first)
        self.assertEqual(value["version"], 1)
        self.assertEqual(value["wait_seconds"], 20)
        self.assertEqual(value["brief"]["kind"], "image")
        self.assertIn("Owner request: calm office", value["brief"]["prompt"])

    def test_submission_uses_only_the_frozen_brief(self) -> None:
        frozen = freeze_business_image_payload(
            request="original request",
            brand_context="Tone: calm",
            country_code="RU",
        )
        expected = SimpleNamespace(id="provider-job-1")
        with patch.object(visual_creatives, "submit_visual", return_value=expected) as submit:
            result = create_business_image_from_frozen_payload(
                provider_payload_json=frozen,
                scope_id="scope-1",
                idempotency_key="stable-key-1",
            )
        self.assertIs(result, expected)
        brief = submit.call_args.args[0]
        self.assertIn("Owner request: original request", brief.prompt)
        self.assertEqual(brief.brand_context, "Tone: calm")
        self.assertEqual(submit.call_args.kwargs["wait_seconds"], 20)

    def test_frozen_payload_rejects_version_or_shape_drift(self) -> None:
        frozen = json.loads(freeze_business_image_payload(request="calm office"))
        for mutate in (
            lambda value: value.__setitem__("version", 2),
            lambda value: value.__setitem__("extra", "changed"),
            lambda value: value.__setitem__("wait_seconds", 61),
        ):
            value = json.loads(json.dumps(frozen))
            mutate(value)
            with self.assertRaises(ValueError):
                create_business_image_from_frozen_payload(
                    provider_payload_json=json.dumps(value),
                    scope_id="scope-1",
                    idempotency_key="stable-key-1",
                )



class CreativeGenerationReceiptRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_creative_experiments.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Creative business")
        self.actor = tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.repo = CreativeGenerationReceiptRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def payload(self, request: str) -> str:
        return freeze_business_image_payload(
            request=request,
            brand_context="Tone: calm, human.",
            country_code="RU",
        )

    def prepare(self, request: str = "calm office"):
        return self.repo.prepare(
            actor=self.actor,
            request_text=request,
            brand_context="Tone: calm, human.",
            country_code="RU",
            provider_payload_json=self.payload(request),
            now="2026-09-10T06:00:00+00:00",
        )

    def test_exact_prepared_payload_is_reused_without_rotating_consent_receipt(self) -> None:
        first = self.prepare()
        second = self.prepare()
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.idempotency_key, first.idempotency_key)
        self.assertEqual(second.provider_payload_json, first.provider_payload_json)

    def test_changed_prepared_request_rotates_receipt_and_invalidates_old_confirmation(self) -> None:
        first = self.prepare("calm office")
        second = self.prepare("bright studio")
        self.assertNotEqual(second.id, first.id)
        self.assertNotEqual(second.idempotency_key, first.idempotency_key)
        self.assertEqual(second.request_text, "bright studio")
        with self.assertRaises(LookupError):
            self.repo.get(actor=self.actor, receipt_id=first.id)
        self.assertEqual(self.repo.get_active(actor=self.actor).id, second.id)

    def test_submission_keeps_exact_frozen_provider_payload(self) -> None:
        prepared = self.prepare()
        submitting = self.repo.begin_submission(actor=self.actor, receipt_id=prepared.id)
        self.assertEqual(submitting.status, CreativeGenerationReceiptStatus.SUBMITTING)
        self.assertEqual(submitting.provider_payload_json, prepared.provider_payload_json)
        self.assertEqual(submitting.idempotency_key, prepared.idempotency_key)

    def test_failed_provider_receipt_is_erased_immediately(self) -> None:
        prepared = self.prepare()
        self.repo.begin_submission(actor=self.actor, receipt_id=prepared.id)
        failed = self.repo.remember_job(
            actor=self.actor,
            receipt_id=prepared.id,
            source_job_id="provider-job-1",
            provider_status="failed",
            now="2026-09-10T06:01:00+00:00",
        )
        self.assertEqual(failed.status, CreativeGenerationReceiptStatus.FAILED)
        self.assertEqual(failed.source_job_id, "provider-job-1")
        with self.assertRaises(LookupError):
            self.repo.get(actor=self.actor, receipt_id=prepared.id)
        self.assertIsNone(self.repo.get_active(actor=self.actor))

    def test_delivered_receipt_is_erased_after_successful_delivery(self) -> None:
        prepared = self.prepare()
        self.repo.begin_submission(actor=self.actor, receipt_id=prepared.id)
        succeeded = self.repo.remember_job(
            actor=self.actor,
            receipt_id=prepared.id,
            source_job_id="provider-job-1",
            provider_status="succeeded",
            now="2026-09-10T06:01:00+00:00",
        )
        self.assertEqual(succeeded.status, CreativeGenerationReceiptStatus.SUCCEEDED)
        self.assertTrue(
            self.repo.claim_delivery(
                actor=self.actor,
                receipt_id=prepared.id,
                now="2026-09-10T06:01:30+00:00",
            )
        )
        delivered = self.repo.mark_delivered(
            actor=self.actor,
            receipt_id=prepared.id,
            now="2026-09-10T06:02:00+00:00",
        )
        self.assertEqual(delivered.status, CreativeGenerationReceiptStatus.DELIVERED)
        with self.assertRaises(LookupError):
            self.repo.get(actor=self.actor, receipt_id=prepared.id)
        self.assertIsNone(self.repo.get_active(actor=self.actor))

    def test_delivery_claim_is_atomic_and_required_before_delete(self) -> None:
        prepared = self.prepare()
        self.repo.begin_submission(actor=self.actor, receipt_id=prepared.id)
        self.repo.remember_job(
            actor=self.actor,
            receipt_id=prepared.id,
            source_job_id="provider-job-1",
            provider_status="succeeded",
        )
        with self.assertRaisesRegex(ValueError, "not claimed"):
            self.repo.mark_delivered(actor=self.actor, receipt_id=prepared.id)
        self.assertTrue(self.repo.claim_delivery(actor=self.actor, receipt_id=prepared.id))
        claimed = self.repo.get(actor=self.actor, receipt_id=prepared.id)
        self.assertTrue(claimed.delivery_claimed_at)
        self.assertFalse(self.repo.claim_delivery(actor=self.actor, receipt_id=prepared.id))

    def test_explicit_redelivery_authorization_clears_claim_once(self) -> None:
        prepared = self.prepare()
        self.repo.begin_submission(actor=self.actor, receipt_id=prepared.id)
        self.repo.remember_job(
            actor=self.actor,
            receipt_id=prepared.id,
            source_job_id="provider-job-1",
            provider_status="succeeded",
        )
        self.assertTrue(self.repo.claim_delivery(actor=self.actor, receipt_id=prepared.id))
        self.assertTrue(self.repo.authorize_redelivery(actor=self.actor, receipt_id=prepared.id))
        self.assertFalse(
            self.repo.get(actor=self.actor, receipt_id=prepared.id).delivery_claimed_at
        )
        self.assertFalse(self.repo.authorize_redelivery(actor=self.actor, receipt_id=prepared.id))

    def test_abandon_succeeded_receipt_erases_it_and_allows_new_request(self) -> None:
        prepared = self.prepare()
        self.repo.begin_submission(actor=self.actor, receipt_id=prepared.id)
        self.repo.remember_job(
            actor=self.actor,
            receipt_id=prepared.id,
            source_job_id="provider-job-1",
            provider_status="succeeded",
        )
        self.assertTrue(self.repo.abandon(actor=self.actor, receipt_id=prepared.id))
        with self.assertRaises(LookupError):
            self.repo.get(actor=self.actor, receipt_id=prepared.id)
        replacement = self.prepare("new visual")
        self.assertEqual(replacement.request_text, "new visual")

    def test_schema_upgrades_existing_receipt_table_with_delivery_claim_column(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                """
                CREATE TABLE creative_generation_receipts(
                    id TEXT PRIMARY KEY, business_id TEXT NOT NULL,
                    created_by_member_id TEXT NOT NULL, request_text TEXT NOT NULL,
                    brand_context TEXT NOT NULL DEFAULT '', country_code TEXT NOT NULL DEFAULT '',
                    provider_payload_json TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    source_job_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )
                """
            )
            clientplatform_creative_experiments.ensure(conn)
            columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(creative_generation_receipts)"
            ).fetchall()}
            self.assertIn("delivery_claimed_at", columns)
        finally:
            conn.close()

    def test_provider_job_id_cannot_change_during_recovery(self) -> None:
        prepared = self.prepare()
        self.repo.begin_submission(actor=self.actor, receipt_id=prepared.id)
        self.repo.remember_job(
            actor=self.actor,
            receipt_id=prepared.id,
            source_job_id="provider-job-1",
            provider_status="running",
        )
        with self.assertRaisesRegex(ValueError, "source job changed"):
            self.repo.remember_job(
                actor=self.actor,
                receipt_id=prepared.id,
                source_job_id="provider-job-2",
                provider_status="running",
            )

    def test_invalid_frozen_payload_is_rejected_before_persistence(self) -> None:
        with self.assertRaisesRegex(ValueError, "provider payload"):
            self.repo.prepare(
                actor=self.actor,
                request_text="calm office",
                brand_context="Tone: calm",
                country_code="RU",
                provider_payload_json="",
            )
        self.assertIsNone(self.repo.get_active(actor=self.actor))


if __name__ == "__main__":
    unittest.main()

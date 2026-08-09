from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from clientplatform.domain.ad_connections import (
    AdProvider,
    new_oauth_state,
    new_pkce_verifier,
)
from clientplatform.domain.ad_spend import (
    AdSpendAuthorizationStatus,
    AdSpendInvariantViolation,
    ProviderBudgetSnapshot,
)
from clientplatform.domain.promotions import (
    PromotionChannel,
    PromotionCreative,
    stable_creative_id,
)
from clientplatform.domain.tenancy import PlatformRole
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.ad_connection_repository import AdConnectionRepository
from clientplatform.infrastructure.ad_credential_vault import InMemoryAdCredentialVault
from clientplatform.infrastructure.ad_spend_repository import AdSpendRepository
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.infrastructure.promotion_repository import PromotionRepository
from clientplatform.integrations.yandex_direct import YandexTokenBundle
from clientplatform.privacy_manifest import validate_clientplatform_privacy_manifest
from services.db.schema import (
    clientplatform_activity,
    clientplatform_ad_connections,
    clientplatform_bookings,
    clientplatform_customers,
    clientplatform_promotions,
    clientplatform_tenancy,
)


_NOW = datetime(2026, 8, 5, 17, 0, tzinfo=timezone.utc)


class AdSpendRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_bookings.ensure(self.conn)
        clientplatform_promotions.ensure(self.conn)
        clientplatform_ad_connections.ensure(self.conn)

        self.tenancy = TenancyRepository(self.conn)
        self.activity = ActivityRepository(self.conn)
        self.bookings = BookingRepository(self.conn)
        self.promotions = PromotionRepository(self.conn)
        self.ads = AdConnectionRepository(
            self.conn,
            vault=InMemoryAdCredentialVault(),
        )
        self.spend = AdSpendRepository(self.conn)

        access = self.tenancy.create_business(owner_user_id=101, name="Сантехник")
        self.owner = self.tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.activity.upsert_profile(
            actor=self.owner,
            activity_description="Ремонтирую сантехнику",
            timezone_name="Europe/Amsterdam",
            now=_NOW.isoformat(),
        )
        capability = self.activity.enable_capability(
            actor=self.owner,
            connector_key="services",
            now=_NOW.isoformat(),
        )
        offering = self.activity.create_offering(
            actor=self.owner,
            capability_id=capability.id,
            title="Замена раковины",
            description="Сниму старую и установлю новую раковину",
            now=_NOW.isoformat(),
        )
        slot = self.bookings.create_slot(
            actor=self.owner,
            offering_id=offering.id,
            local_start="10.08.2026 12:00",
            duration_minutes=60,
            now=_NOW.isoformat(),
        )
        creative = PromotionCreative(
            creative_id=stable_creative_id("sink", "website"),
            headline="Замена раковины",
            primary_text="Свободное время у сантехника. Запишитесь онлайн.",
            description="60 минут",
        )
        self.promotion, _ = self.promotions.create_or_refresh_campaign(
            actor=self.owner,
            slot_id=slot.slot.id,
            channel=PromotionChannel.WEBSITE,
            creative=creative,
            now=_NOW.isoformat(),
        )
        self.connection = self._activate_connection()
        self.submitted_job = self._submitted_job()

    def tearDown(self) -> None:
        self.conn.close()

    def _activate_connection(self):
        state = new_oauth_state()
        verifier = new_pkce_verifier()
        self.ads.create_oauth_session(
            actor=self.owner,
            provider=AdProvider.YANDEX_DIRECT,
            state=state,
            verifier=verifier,
            now=_NOW,
        )
        session, _ = self.ads.consume_oauth_session(state=state, now=_NOW)
        return self.ads.activate_oauth_connection(
            session=session,
            external_account_id="100500",
            external_login="vasya",
            token_bundle_json=YandexTokenBundle(
                access_token="secret-token",
                token_type="bearer",
                expires_in=3600,
                refresh_token="refresh-token",
                scope=("direct:api",),
            ).to_json(),
            permissions=("campaigns.read", "adgroups.write", "ads.write"),
            now=_NOW,
        )

    def _new_publication_job(self):
        return self.ads.create_or_get_job(
            actor=self.owner,
            promotion_campaign_id=self.promotion.id,
            connection_id=self.connection.id,
            external_campaign_id="6001",
            external_campaign_name="Локальные услуги",
            region_ids=(47, 213),
            source_url="https://t.me/clientplatform_bot?start=cpa_source",
            title=self.promotion.creative.headline,
            text=self.promotion.creative.primary_text,
            creative_id=self.promotion.creative.creative_id,
            now=_NOW,
        )

    def _submitted_job(self):
        draft = self._new_publication_job()
        self.ads.queue_job(actor=self.owner, job_id=draft.id, now=_NOW)
        claimed = self.ads.claim_due_job(now=_NOW)
        self.assertIsNotNone(claimed)
        job, lock_token = claimed
        return self.ads.complete_job(
            job=job,
            lock_token=lock_token,
            external_ad_group_id="7001",
            external_ad_id="8001",
            now=_NOW,
        )

    def _snapshot(self) -> ProviderBudgetSnapshot:
        return ProviderBudgetSnapshot(
            provider=AdProvider.YANDEX_DIRECT,
            connection_id=self.connection.id,
            external_account_id="100500",
            external_campaign_id="6001",
            currency="RUB",
            available_budget_minor=50_000,
            spent_today_minor=1_000,
            campaign_status="ON",
            strategy="HIGHEST_POSITION",
            launch_eligible=True,
            provider_version="campaign-v18",
            captured_at=_NOW,
            valid_until=_NOW + timedelta(minutes=15),
        )

    def _draft(self):
        return self.spend.create_or_get_draft(
            actor=self.owner,
            publication_job_id=self.submitted_job.id,
            snapshot=self._snapshot(),
            region_ids=(47, 213),
            hard_cap_minor=20_000,
            daily_cap_minor=5_000,
            authorization_expires_at=_NOW + timedelta(minutes=10),
            now=_NOW,
        )

    def test_draft_is_idempotent_and_requires_submitted_provider_draft(self) -> None:
        first = self._draft()
        second = self._draft()
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.status, AdSpendAuthorizationStatus.DRAFT)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM ad_spend_authorizations"
            ).fetchone()[0],
            1,
        )

        self.conn.execute(
            "UPDATE ad_publication_jobs SET status='draft' WHERE id=?",
            (self.submitted_job.id,),
        )
        with self.assertRaisesRegex(
            AdSpendInvariantViolation,
            "provider-created DRAFT",
        ):
            self.spend.create_or_get_draft(
                actor=self.owner,
                publication_job_id=self.submitted_job.id,
                snapshot=self._snapshot(),
                region_ids=(47,),
                hard_cap_minor=10_000,
                daily_cap_minor=2_000,
                authorization_expires_at=_NOW + timedelta(minutes=10),
                now=_NOW,
            )

    def test_snapshot_must_match_connected_external_account(self) -> None:
        valid = self._snapshot()
        foreign_account = ProviderBudgetSnapshot(
            provider=valid.provider,
            connection_id=valid.connection_id,
            external_account_id="999999",
            external_campaign_id=valid.external_campaign_id,
            currency=valid.currency,
            available_budget_minor=valid.available_budget_minor,
            spent_today_minor=valid.spent_today_minor,
            campaign_status=valid.campaign_status,
            strategy=valid.strategy,
            launch_eligible=valid.launch_eligible,
            provider_version=valid.provider_version,
            captured_at=valid.captured_at,
            valid_until=valid.valid_until,
        )

        with self.assertRaisesRegex(
            AdSpendInvariantViolation,
            "provider snapshot account does not match connection",
        ):
            self.spend.create_or_get_draft(
                actor=self.owner,
                publication_job_id=self.submitted_job.id,
                snapshot=foreign_account,
                region_ids=(47, 213),
                hard_cap_minor=20_000,
                daily_cap_minor=5_000,
                authorization_expires_at=_NOW + timedelta(minutes=10),
                now=_NOW,
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM ad_spend_authorizations"
            ).fetchone()[0],
            0,
        )

    def test_consent_is_idempotent_and_persists_exactly_one_receipt(self) -> None:
        draft = self._draft()
        awaiting = self.spend.request_consent(
            actor=self.owner,
            authorization_id=draft.id,
            now=_NOW + timedelta(seconds=1),
        )
        repeated = self.spend.request_consent(
            actor=self.owner,
            authorization_id=draft.id,
            now=_NOW + timedelta(seconds=2),
        )
        self.assertEqual(awaiting.id, repeated.id)
        self.assertEqual(
            repeated.status,
            AdSpendAuthorizationStatus.AWAITING_CONSENT,
        )

        authorized, receipt = self.spend.authorize(
            actor=self.owner,
            authorization_id=draft.id,
            receipt_id=str(uuid4()),
            now=_NOW + timedelta(seconds=3),
        )
        repeated_authorization, repeated_receipt = self.spend.authorize(
            actor=self.owner,
            authorization_id=draft.id,
            receipt_id=str(uuid4()),
            now=_NOW + timedelta(seconds=4),
        )
        self.assertEqual(authorized.id, repeated_authorization.id)
        self.assertEqual(receipt.id, repeated_receipt.id)
        self.assertEqual(receipt.receipt_hash, repeated_receipt.receipt_hash)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM ad_spend_consent_receipts"
            ).fetchone()[0],
            1,
        )
        row = self.conn.execute(
            "SELECT status, row_version, consent_receipt_id "
            "FROM ad_spend_authorizations WHERE id=?",
            (draft.id,),
        ).fetchone()
        self.assertEqual(row["status"], "authorized")
        self.assertEqual(row["row_version"], 2)
        self.assertEqual(row["consent_receipt_id"], receipt.id)
        actions = [
            row[0]
            for row in self.conn.execute(
                "SELECT action FROM ad_audit_events "
                "WHERE subject_id=? ORDER BY created_at, action",
                (draft.id,),
            ).fetchall()
        ]
        self.assertEqual(
            set(actions),
            {
                "ad_spend_authorization_created",
                "ad_spend_consent_requested",
                "ad_spend_consent_granted",
            },
        )

    def test_non_owner_and_cross_tenant_access_fail_closed(self) -> None:
        draft = self._draft()
        self.tenancy.grant_member(
            actor=self.owner,
            user_id=202,
            role=PlatformRole.ADMINISTRATOR,
            now=_NOW.isoformat(),
        )
        administrator = self.tenancy.resolve_context(
            user_id=202,
            business_id=self.owner.business_id,
        )
        with self.assertRaisesRegex(AdSpendInvariantViolation, "owner role"):
            self.spend.get(actor=administrator, authorization_id=draft.id)

        other = self.tenancy.create_business(owner_user_id=303, name="Другой бизнес")
        other_owner = self.tenancy.resolve_context(
            user_id=303,
            business_id=other.business.id,
        )
        with self.assertRaisesRegex(AdSpendInvariantViolation, "not found for business"):
            self.spend.get(actor=other_owner, authorization_id=draft.id)

    def test_failed_authorization_cas_leaves_no_orphan_receipt(self) -> None:
        draft = self._draft()
        self.spend.request_consent(
            actor=self.owner,
            authorization_id=draft.id,
            now=_NOW + timedelta(seconds=1),
        )
        self.conn.execute(
            """
            CREATE TRIGGER reject_ad_spend_authorize
            BEFORE UPDATE OF status ON ad_spend_authorizations
            WHEN NEW.status='authorized'
            BEGIN
                SELECT RAISE(ABORT, 'authorization blocked');
            END
            """
        )
        with self.assertRaises(sqlite3.DatabaseError):
            self.spend.authorize(
                actor=self.owner,
                authorization_id=draft.id,
                receipt_id=str(uuid4()),
                now=_NOW + timedelta(seconds=2),
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM ad_spend_consent_receipts"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM ad_spend_authorizations WHERE id=?",
                (draft.id,),
            ).fetchone()[0],
            "awaiting_consent",
        )

    def test_tampered_snapshot_and_receipt_are_detected_on_read(self) -> None:
        draft = self._draft()
        self.spend.request_consent(
            actor=self.owner,
            authorization_id=draft.id,
            now=_NOW + timedelta(seconds=1),
        )
        authorized, _ = self.spend.authorize(
            actor=self.owner,
            authorization_id=draft.id,
            receipt_id=str(uuid4()),
            now=_NOW + timedelta(seconds=2),
        )
        original_terms = self.conn.execute(
            "SELECT terms_json FROM ad_spend_consent_receipts "
            "WHERE authorization_id=?",
            (authorized.id,),
        ).fetchone()[0]
        self.conn.execute(
            "UPDATE ad_spend_consent_receipts SET terms_json='{}' "
            "WHERE authorization_id=?",
            (authorized.id,),
        )
        with self.assertRaisesRegex(AdSpendInvariantViolation, "terms hash"):
            self.spend.get(actor=self.owner, authorization_id=authorized.id)
        self.conn.execute(
            "UPDATE ad_spend_consent_receipts SET terms_json=? "
            "WHERE authorization_id=?",
            (original_terms, authorized.id),
        )

        original_snapshot = self.conn.execute(
            "SELECT snapshot_json FROM ad_spend_authorizations WHERE id=?",
            (authorized.id,),
        ).fetchone()[0]
        self.conn.execute(
            "UPDATE ad_spend_authorizations SET snapshot_json='{}' WHERE id=?",
            (authorized.id,),
        )
        with self.assertRaisesRegex(
            AdSpendInvariantViolation,
            "stored provider snapshot",
        ):
            self.spend.get(actor=self.owner, authorization_id=authorized.id)
        self.conn.execute(
            "UPDATE ad_spend_authorizations SET snapshot_json=? WHERE id=?",
            (original_snapshot, authorized.id),
        )

    def test_schema_and_privacy_manifest_cover_new_tenant_tables(self) -> None:
        clientplatform_ad_connections.ensure(self.conn)
        report = validate_clientplatform_privacy_manifest(
            self.conn,
            require_complete=False,
        )
        self.assertTrue(report.ok)
        self.assertIn("ad_spend_authorizations", report.discovered_business_tables)
        self.assertIn("ad_spend_consent_receipts", report.discovered_business_tables)
        columns = {
            row["name"]: row["type"]
            for row in self.conn.execute(
                "PRAGMA table_info(ad_spend_authorizations)"
            ).fetchall()
        }
        self.assertEqual(columns["hard_cap_minor"].upper(), "BIGINT")
        self.assertEqual(columns["daily_cap_minor"].upper(), "BIGINT")
        self.assertEqual(columns["row_version"].upper(), "BIGINT")


if __name__ == "__main__":
    unittest.main()

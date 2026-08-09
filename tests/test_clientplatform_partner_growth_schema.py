from __future__ import annotations

import sqlite3
import unittest
from uuid import uuid4

from clientplatform.application.partner_scoring import score_partner
from clientplatform.domain.partners import (
    ContactBasis,
    PartnerCampaignGoal,
    PartnerCandidate,
    PartnerChannel,
    PartnerContentPack,
)
from clientplatform.domain.tenancy import PlatformRole, TenantPermissionDenied
from clientplatform.infrastructure.partner_repository import PartnerRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.privacy_manifest import TENANT_POLICIES
from services.db.schema import clientplatform_partners, clientplatform_tenancy


class PartnerGrowthSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_partners.ensure(self.conn)
        access = TenancyRepository(self.conn).create_business(
            owner_user_id=101,
            name="Test business",
        )
        self.owner = TenancyRepository(self.conn).resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.repo = PartnerRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _campaign(self, name: str):
        return self.repo.create_campaign(
            actor=self.owner,
            name=name,
            goal=PartnerCampaignGoal(
                target_count=10,
                audience_terms=("psychology",),
                target_url="https://example.test/offer",
            ),
        )

    def _candidate(self, campaign):
        provisional = PartnerCandidate(
            id=str(uuid4()),
            business_id=campaign.business_id,
            campaign_id=campaign.id,
            name="Partner",
            source_url="https://example.test/partner",
            audience_summary="psychology",
            recent_topic="stress",
            channel=PartnerChannel.EMAIL,
            contact_value="partner@example.test",
            contact_basis=ContactBasis.PUBLIC_BUSINESS_CONTACT,
            follower_count=5000,
        )
        score = score_partner(provisional, campaign.goal)
        return self.repo.upsert_candidate(
            actor=self.owner,
            campaign=campaign,
            name=provisional.name,
            source_url=provisional.source_url,
            audience_summary=provisional.audience_summary,
            recent_topic=provisional.recent_topic,
            channel=provisional.channel,
            contact_value=provisional.contact_value,
            contact_basis=provisional.contact_basis,
            follower_count=provisional.follower_count,
            tags=provisional.tags,
            competitor=False,
            score=score,
        )

    def test_child_rows_cannot_pair_candidate_with_another_campaign(self) -> None:
        campaign_a = self._campaign("A")
        campaign_b = self._campaign("B")
        candidate = self._candidate(campaign_a)
        pack = PartnerContentPack(
            candidate_id=candidate.id,
            subject="Subject",
            outreach_message="Proposal",
            ready_post="Ready post",
            followup_message="Follow-up",
            collaboration_angle="Guest material",
            cta="Reply",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.save_content_pack(
                actor=self.owner,
                campaign_id=campaign_b.id,
                pack=pack,
            )

    def test_analyst_gets_aggregates_but_not_candidate_contacts(self) -> None:
        campaign = self._campaign("Analytics")
        self._candidate(campaign)
        member_id = str(uuid4())
        now = "2026-08-10T00:00:00+00:00"
        self.conn.execute(
            """
            INSERT INTO business_members(
                id, business_id, user_id, role, status,
                created_at, updated_at, revoked_at
            ) VALUES(?, ?, ?, ?, 'active', ?, ?, NULL)
            """,
            (
                member_id,
                self.owner.business_id,
                202,
                PlatformRole.ANALYST.value,
                now,
                now,
            ),
        )
        analyst = TenancyRepository(self.conn).resolve_context(
            user_id=202,
            business_id=self.owner.business_id,
        )
        stats = self.repo.stats(actor=analyst, campaign_id=campaign.id)
        self.assertEqual(stats.candidates, 1)
        with self.assertRaises(TenantPermissionDenied):
            self.repo.list_candidates(actor=analyst, campaign_id=campaign.id)

    def test_referral_event_key_rejects_raw_numeric_identity_shape(self) -> None:
        campaign = self._campaign("Referral")
        candidate = self._candidate(campaign)
        with self.assertRaises(ValueError):
            self.repo.record_referral_event(
                referral_token=candidate.referral_token,
                event_type="opened",
                event_key="123456789",
            )
        self.assertTrue(
            self.repo.record_referral_event(
                referral_token=candidate.referral_token,
                event_type="opened",
                event_key="opaque_event_token_12345",
            )
        )
        self.assertFalse(
            self.repo.record_referral_event(
                referral_token=candidate.referral_token,
                event_type="opened",
                event_key="opaque_event_token_12345",
            )
        )

    def test_privacy_manifest_classifies_every_partner_table(self) -> None:
        expected = {
            "partner_campaigns",
            "partner_candidates",
            "partner_content_packs",
            "partner_placements",
            "partner_referral_events",
        }
        self.assertTrue(expected.issubset(TENANT_POLICIES))


if __name__ == "__main__":
    unittest.main()

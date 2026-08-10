from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application.partner_attribution import (
    PartnerAttributionWriteError,
    record_partner_referral_open,
    record_partner_referral_result,
    resolve_partner_referral,
)
from clientplatform.application.partner_scoring import score_partner
from clientplatform.domain.partners import (
    ContactBasis,
    PartnerCampaignGoal,
    PartnerCandidate,
    PartnerChannel,
)
from clientplatform.infrastructure.partner_repository import PartnerRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import clientplatform_partners, clientplatform_tenancy


class PartnerAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_partners.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=501, name="Attribution")
        self.actor = tenancy.resolve_context(
            user_id=501,
            business_id=access.business.id,
        )
        repo = PartnerRepository(self.conn)
        self.campaign = repo.create_campaign(
            actor=self.actor,
            name="Referral",
            goal=PartnerCampaignGoal(
                target_count=1,
                audience_terms=("psychology",),
            ),
        )
        provisional = PartnerCandidate(
            id=str(uuid4()),
            business_id=self.actor.business_id,
            campaign_id=self.campaign.id,
            name="Referral partner",
            source_url="https://example.test/referral-partner",
            audience_summary="psychology audience",
            recent_topic="stress",
            channel=PartnerChannel.VK,
            contact_value="https://vk.com/referral_partner",
            contact_basis=ContactBasis.PUBLIC_BUSINESS_CONTACT,
            follower_count=1000,
        )
        self.candidate = repo.upsert_candidate(
            actor=self.actor,
            campaign=self.campaign,
            name=provisional.name,
            source_url=provisional.source_url,
            audience_summary=provisional.audience_summary,
            recent_topic=provisional.recent_topic,
            channel=provisional.channel,
            contact_value=provisional.contact_value,
            contact_basis=provisional.contact_basis,
            follower_count=provisional.follower_count,
            tags=("referral",),
            competitor=False,
            score=score_partner(provisional, self.campaign.goal),
        )

    def tearDown(self) -> None:
        self.conn.close()

    @contextmanager
    def _db(self):
        yield self.conn
        self.conn.commit()

    def test_open_and_result_are_distinct_and_result_is_business_event_deduped(self) -> None:
        with (
            patch("clientplatform.application.partner_attribution.get_db", self._db),
            patch("clientplatform.application.partner_attribution.get_db_ro", self._db),
        ):
            landing = resolve_partner_referral(
                referral_token=self.candidate.referral_token,
            )
            self.assertEqual(landing.business_id, self.actor.business_id)
            self.assertEqual(landing.candidate_id, self.candidate.id)

            self.assertTrue(
                record_partner_referral_open(
                    referral_token=self.candidate.referral_token,
                )
            )
            self.assertTrue(
                record_partner_referral_open(
                    referral_token=self.candidate.referral_token,
                )
            )
            result_key = "booking_11111111-1111-4111-8111-111111111111"
            self.assertTrue(
                record_partner_referral_result(
                    referral_token=self.candidate.referral_token,
                    result_key=result_key,
                )
            )
            self.assertFalse(
                record_partner_referral_result(
                    referral_token=self.candidate.referral_token,
                    result_key=result_key,
                )
            )

        stats = PartnerRepository(self.conn).stats(
            actor=self.actor,
            campaign_id=self.campaign.id,
        )
        self.assertEqual(stats.attributed_visits, 2)
        self.assertEqual(stats.attributed_results, 1)

    def test_result_key_must_be_bounded_and_non_empty(self) -> None:
        with patch("clientplatform.application.partner_attribution.get_db", self._db):
            with self.assertRaises(ValueError):
                record_partner_referral_result(
                    referral_token=self.candidate.referral_token,
                    result_key="",
                )
            with self.assertRaises(ValueError):
                record_partner_referral_result(
                    referral_token=self.candidate.referral_token,
                    result_key="x" * 161,
                )

    def test_storage_failure_is_normalized_at_application_boundary(self) -> None:
        @contextmanager
        def broken_db():
            raise sqlite3.OperationalError("storage unavailable")
            yield  # pragma: no cover

        with patch("clientplatform.application.partner_attribution.get_db", broken_db):
            with self.assertRaisesRegex(
                PartnerAttributionWriteError,
                "partner_attribution_write_failed",
            ):
                record_partner_referral_open(
                    referral_token=self.candidate.referral_token,
                )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from uuid import uuid4

from clientplatform.application.partner_copy import (
    DeterministicPartnerCopyGenerator,
    PartnerCopyContext,
    validate_partner_content,
)
from clientplatform.application.partner_growth import PartnerGrowthService
from clientplatform.application.partner_scoring import score_partner
from clientplatform.domain.partners import (
    ContactBasis,
    PartnerAutomationMode,
    PartnerCampaign,
    PartnerCampaignGoal,
    PartnerCampaignStatus,
    PartnerCandidate,
    PartnerChannel,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.integrations.partner_discovery import (
    CompositePartnerDiscovery,
    PartnerDiscoveryProviderError,
    PartnerDiscoveryQuery,
    PartnerDiscoveryUnavailable,
)


def _campaign() -> PartnerCampaign:
    return PartnerCampaign(
        id=str(uuid4()),
        business_id=str(uuid4()),
        name="Эксперимент 300",
        goal=PartnerCampaignGoal(
            target_count=300,
            budget_minor=0,
            event_title="Гипноз без мистики",
            target_url="https://example.test/webinar",
            audience_terms=("психология", "тревога", "саморазвитие"),
        ),
        automation_mode=PartnerAutomationMode.AUTOPILOT,
        status=PartnerCampaignStatus.ACTIVE,
        created_by_member_id=str(uuid4()),
        created_at="2026-08-09T19:00:00+00:00",
        updated_at="2026-08-09T19:00:00+00:00",
    )


def _candidate(
    campaign: PartnerCampaign,
    *,
    basis: ContactBasis = ContactBasis.UNKNOWN,
) -> PartnerCandidate:
    return PartnerCandidate(
        id=str(uuid4()),
        business_id=campaign.business_id,
        campaign_id=campaign.id,
        name="Психология без шума",
        source_url="https://example.test/channel",
        audience_summary="Психология, тревога, саморазвитие",
        recent_topic="Почему тревога усиливается вечером",
        channel=PartnerChannel.EMAIL,
        contact_value=("partner@example.test" if basis != ContactBasis.NONE else ""),
        contact_basis=basis,
        follower_count=8500,
        tags=("психология", "тревога"),
    )


class PartnerGrowthDomainTests(unittest.TestCase):
    def test_public_business_contact_is_rankable_but_never_send_authority(self) -> None:
        campaign = _campaign()
        candidate = _candidate(
            campaign,
            basis=ContactBasis.PUBLIC_BUSINESS_CONTACT,
        )
        self.assertFalse(candidate.first_contact_permitted)
        score = score_partner(candidate, campaign.goal)
        self.assertGreaterEqual(score.total, 55)
        self.assertIn("no_first_contact_basis", score.reasons)

    def test_opt_in_is_explicit_first_contact_authority(self) -> None:
        campaign = _campaign()
        candidate = _candidate(campaign, basis=ContactBasis.OPTED_IN)
        self.assertTrue(candidate.first_contact_permitted)
        self.assertIn(
            "contact_basis_allows_first_contact",
            score_partner(candidate, campaign.goal).reasons,
        )

    def test_deterministic_copy_uses_known_topic_without_invented_familiarity(self) -> None:
        campaign = _campaign()
        candidate = _candidate(
            campaign,
            basis=ContactBasis.PUBLIC_BUSINESS_CONTACT,
        )
        pack = DeterministicPartnerCopyGenerator().generate(
            PartnerCopyContext(
                business_name="ClientPlatform",
                activity_description="Практики управления состоянием",
                offerings=("Практический вебинар",),
                campaign=campaign,
                candidate=candidate,
                public_target_url="https://example.test/p/abc",
            )
        )
        validate_partner_content(pack)
        self.assertIn("тревога", pack.outreach_message.casefold())
        self.assertIn("https://example.test/p/abc", pack.ready_post)
        self.assertNotIn("давно слежу", pack.outreach_message.casefold())
        self.assertNotIn("мы изучили ваш канал", pack.outreach_message.casefold())

    def test_goal_rejects_negative_budget(self) -> None:
        with self.assertRaises(ValueError):
            PartnerCampaignGoal(target_count=10, budget_minor=-1)

    def test_all_configured_provider_failures_are_not_reported_as_zero(self) -> None:
        class BrokenProvider:
            provider_name = "broken"

            def discover(self, query: PartnerDiscoveryQuery):
                del query
                raise PartnerDiscoveryProviderError("unavailable")

        discovery = CompositePartnerDiscovery((BrokenProvider(),))
        with self.assertRaisesRegex(PartnerDiscoveryUnavailable, "broken"):
            discovery.discover(
                PartnerDiscoveryQuery(("psychology",), limit=5)
            )

    def test_unconfigured_discovery_fails_before_campaign_persistence(self) -> None:
        service = PartnerGrowthService(
            discovery=CompositePartnerDiscovery(()),
        )
        actor = TenantContext(
            business_id=str(uuid4()),
            user_id=101,
            membership_id=str(uuid4()),
            role=PlatformRole.OWNER,
        )
        with self.assertRaisesRegex(
            PartnerDiscoveryUnavailable,
            "not configured",
        ):
            service.start(
                actor=actor,
                name="Should not persist",
                goal=PartnerCampaignGoal(
                    target_count=10,
                    audience_terms=("psychology",),
                ),
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence
from uuid import uuid4

from clientplatform.application.partner_copy import (
    DeterministicPartnerCopyGenerator,
    PartnerCopyContext,
    PartnerCopyGenerator,
)
from clientplatform.application.partner_scoring import score_partner
from clientplatform.domain.partners import (
    PartnerAutomationMode,
    PartnerCampaign,
    PartnerCampaignGoal,
    PartnerCampaignStats,
    PartnerCampaignStatus,
    PartnerCandidate,
    PartnerInvariantViolation,
    normalize_public_contact,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.partner_repository import PartnerRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.integrations.partner_discovery import (
    CompositePartnerDiscovery,
    DiscoveredPartner,
    PartnerDiscoveryQuery,
)
from services.db import get_db, get_db_ro


@dataclass(frozen=True, slots=True)
class PartnerPreparationRun:
    """Result of read-only discovery plus local preparation; never an external send."""

    campaign: PartnerCampaign
    discovered: int
    prepared: int
    top_candidates: tuple[PartnerCandidate, ...]
    stats: PartnerCampaignStats


@dataclass(frozen=True, slots=True)
class PartnerGrowthPolicy:
    min_fit_score: float = 55.0
    max_candidates_per_run: int = 50

    def __post_init__(self) -> None:
        if not 0 <= float(self.min_fit_score) <= 100:
            raise ValueError("min_fit_score must be between 0 and 100")
        if not 1 <= int(self.max_candidates_per_run) <= 500:
            raise ValueError("max_candidates_per_run must be between 1 and 500")


class PartnerGrowthService:
    """Prepare partner opportunities without owning external-send authority.

    The uploaded feature kit included a direct outbound step and an application-
    level daily quota. That is intentionally not composed here: a production
    automatic send must go through ClientPlatform's canonical connection +
    outbox/lease/idempotency contour. This service stops at durable preparation.
    """

    def __init__(
        self,
        *,
        discovery: CompositePartnerDiscovery,
        copy_generator: PartnerCopyGenerator | None = None,
        policy: PartnerGrowthPolicy | None = None,
    ) -> None:
        self._discovery = discovery
        self._copy = copy_generator or DeterministicPartnerCopyGenerator()
        self._policy = policy or PartnerGrowthPolicy()

    @property
    def discovery_configured(self) -> bool:
        return self._discovery.configured

    def start(
        self,
        *,
        actor: TenantContext,
        name: str,
        goal: PartnerCampaignGoal,
        automation_mode: PartnerAutomationMode | str = PartnerAutomationMode.CAUTIOUS,
    ) -> PartnerPreparationRun:
        # automation_mode is persisted as future execution intent only. It does
        # not grant this preparation service authority to contact anyone.
        with get_db() as conn:
            current = TenancyRepository(conn).resolve_context(
                user_id=actor.user_id,
                business_id=actor.business_id,
            )
            campaign = PartnerRepository(conn).create_campaign(
                actor=current,
                name=name,
                goal=goal,
                automation_mode=automation_mode,
            )
        return self.run(actor=actor, campaign_id=campaign.id)

    def run(
        self,
        *,
        actor: TenantContext,
        campaign_id: str,
    ) -> PartnerPreparationRun:
        profile, offering_titles, campaign = self._load_context(
            actor=actor,
            campaign_id=campaign_id,
        )
        if campaign.status != PartnerCampaignStatus.ACTIVE:
            raise PartnerInvariantViolation("partner campaign is not active")
        terms = campaign.goal.audience_terms or self._fallback_terms(
            profile.activity_description,
            offering_titles,
        )
        discovered = self._discovery.discover(
            PartnerDiscoveryQuery(
                terms=terms,
                limit=self._policy.max_candidates_per_run,
            )
        )
        self._persist_and_prepare(
            actor=actor,
            campaign=campaign,
            rows=discovered,
            business_name=profile.business_name,
            activity_description=profile.activity_description,
            offering_titles=offering_titles,
        )
        with get_db_ro() as conn:
            repo = PartnerRepository(conn)
            top = repo.list_candidates(
                actor=actor,
                campaign_id=campaign.id,
                limit=self._policy.max_candidates_per_run,
            )
            stats = repo.stats(actor=actor, campaign_id=campaign.id)
        return PartnerPreparationRun(
            campaign=campaign,
            discovered=len(discovered),
            prepared=len(top),
            top_candidates=tuple(top[:20]),
            stats=stats,
        )

    def _load_context(
        self,
        *,
        actor: TenantContext,
        campaign_id: str,
    ) -> tuple[_BusinessProfileView, tuple[str, ...], PartnerCampaign]:
        with get_db_ro() as conn:
            current = TenancyRepository(conn).resolve_context(
                user_id=actor.user_id,
                business_id=actor.business_id,
            )
            campaign = PartnerRepository(conn).get_campaign(
                actor=current,
                campaign_id=campaign_id,
            )
            activities = ActivityRepository(conn)
            profile = activities.get_profile(actor=current)
            business_row = conn.execute(
                "SELECT name FROM businesses WHERE id=? LIMIT 1",
                (current.business_id,),
            ).fetchone()
            if business_row is None:
                business_name = "Бизнес"
            elif hasattr(business_row, "keys"):
                business_name = str(business_row["name"])
            else:
                business_name = str(business_row[0])
            offering_titles: list[str] = []
            for capability in activities.list_capabilities(actor=current):
                for offering in activities.list_offerings(
                    actor=current,
                    capability_id=capability.id,
                ):
                    offering_titles.append(offering.title)
                    if len(offering_titles) >= 12:
                        break
                if len(offering_titles) >= 12:
                    break
            return (
                _BusinessProfileView(
                    business_name=business_name,
                    activity_description=profile.activity_description,
                ),
                tuple(offering_titles),
                campaign,
            )

    def _persist_and_prepare(
        self,
        *,
        actor: TenantContext,
        campaign: PartnerCampaign,
        rows: Sequence[DiscoveredPartner],
        business_name: str,
        activity_description: str,
        offering_titles: tuple[str, ...],
    ) -> None:
        with get_db() as conn:
            repo = PartnerRepository(conn)
            for row in rows:
                provisional = PartnerCandidate(
                    id=str(uuid4()),
                    business_id=actor.business_id,
                    campaign_id=campaign.id,
                    name=row.name,
                    source_url=row.source_url,
                    audience_summary=row.audience_summary,
                    recent_topic=row.recent_topic,
                    channel=row.channel,
                    contact_value=normalize_public_contact(
                        row.channel,
                        row.contact_value,
                    ),
                    contact_basis=row.contact_basis,
                    follower_count=row.follower_count,
                    tags=row.tags,
                    competitor=row.competitor,
                )
                fit = score_partner(provisional, campaign.goal)
                if fit.total < self._policy.min_fit_score:
                    continue
                candidate = repo.upsert_candidate(
                    actor=actor,
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
                    competitor=provisional.competitor,
                    score=fit,
                )
                pack = self._copy.generate(
                    PartnerCopyContext(
                        business_name=business_name,
                        activity_description=activity_description,
                        offerings=offering_titles,
                        campaign=campaign,
                        candidate=candidate,
                        public_target_url=campaign.goal.target_url,
                    )
                )
                repo.save_content_pack(
                    actor=actor,
                    campaign_id=campaign.id,
                    pack=pack,
                )

    @staticmethod
    def _fallback_terms(
        activity_description: str,
        offerings: Sequence[str],
    ) -> tuple[str, ...]:
        words: list[str] = []
        for source in (activity_description, *offerings):
            for token in (
                str(source or "").replace("/", " ").replace(",", " ").split()
            ):
                normalized = token.strip(".():;!?\"'").casefold()
                if len(normalized) >= 5 and normalized not in words:
                    words.append(normalized)
                if len(words) >= 8:
                    return tuple(words)
        return tuple(words or ("бизнес",))


@dataclass(frozen=True, slots=True)
class _BusinessProfileView:
    business_name: str
    activity_description: str


__all__ = [
    "PartnerGrowthPolicy",
    "PartnerGrowthService",
    "PartnerPreparationRun",
]

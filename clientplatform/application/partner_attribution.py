from __future__ import annotations

import sqlite3
from dataclasses import dataclass

try:
    from psycopg import Error as PostgresError
except ImportError:  # pragma: no cover - dependency-light boundary
    class PostgresError(Exception):
        """Fallback type used when the optional Postgres driver is absent."""


from clientplatform.infrastructure.partner_repository import PartnerRepository
from services.db import get_db, get_db_ro


class PartnerAttributionWriteError(RuntimeError):
    """A referral evidence write could not be persisted safely."""


@dataclass(frozen=True, slots=True)
class PartnerReferralLanding:
    business_id: str
    campaign_id: str
    candidate_id: str
    candidate_name: str
    referral_token: str


def resolve_partner_referral(*, referral_token: str) -> PartnerReferralLanding:
    with get_db_ro() as conn:
        candidate, campaign = PartnerRepository(conn).resolve_public_referral(
            referral_token=referral_token,
        )
        return PartnerReferralLanding(
            business_id=candidate.business_id,
            campaign_id=campaign.id,
            candidate_id=candidate.id,
            candidate_name=candidate.name,
            referral_token=candidate.referral_token,
        )


def _record_partner_referral_event(
    *,
    referral_token: str,
    event_type: str,
    event_key: str | None = None,
) -> bool:
    try:
        with get_db() as conn:
            return PartnerRepository(conn).record_referral_event(
                referral_token=referral_token,
                event_type=event_type,
                event_key=event_key,
            )
    except sqlite3.Error as exc:
        raise PartnerAttributionWriteError("partner_attribution_write_failed") from exc
    except PostgresError as exc:
        raise PartnerAttributionWriteError("partner_attribution_write_failed") from exc
    except TimeoutError as exc:
        raise PartnerAttributionWriteError("partner_attribution_write_failed") from exc
    except OSError as exc:
        raise PartnerAttributionWriteError("partner_attribution_write_failed") from exc
    except RuntimeError as exc:
        raise PartnerAttributionWriteError("partner_attribution_write_failed") from exc


def record_partner_referral_open(*, referral_token: str) -> bool:
    """Record a link opening with a repository-generated opaque event key."""

    return _record_partner_referral_event(
        referral_token=referral_token,
        event_type="opened",
    )


def record_partner_referral_result(
    *,
    referral_token: str,
    result_key: str,
) -> bool:
    """Record one confirmed downstream result under a stable non-personal key."""

    key = str(result_key or "").strip()
    if not key or len(key) > 160:
        raise ValueError("partner result key must be a bounded non-empty value")
    return _record_partner_referral_event(
        referral_token=referral_token,
        event_type="result",
        event_key=key,
    )


__all__ = [
    "PartnerAttributionWriteError",
    "PartnerReferralLanding",
    "record_partner_referral_open",
    "record_partner_referral_result",
    "resolve_partner_referral",
]

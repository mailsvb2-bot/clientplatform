from __future__ import annotations

from clientplatform.domain.activity import BusinessProfile
from clientplatform.domain.tenancy import (
    BusinessAccess,
    OwnerOnboardingSession,
    OwnerOnboardingStep,
)
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.postgres_safe_activity_repository import ActivityRepository
from services.db import get_db, get_db_ro


def get_owner_onboarding_session(
    *, user_id: int, platform: str
) -> OwnerOnboardingSession | None:
    with get_db_ro() as conn:
        return TenancyRepository(conn).get_owner_onboarding_session(
            user_id=user_id, platform=platform
        )


def begin_business_name_onboarding(
    *, user_id: int, platform: str
) -> OwnerOnboardingSession:
    with get_db() as conn:
        return TenancyRepository(conn).set_owner_onboarding_session(
            user_id=user_id,
            platform=platform,
            step=OwnerOnboardingStep.BUSINESS_NAME,
        )


def cancel_owner_onboarding(*, user_id: int, platform: str) -> None:
    with get_db() as conn:
        TenancyRepository(conn).clear_owner_onboarding_session(
            user_id=user_id, platform=platform
        )


def create_business_from_onboarding(
    *, owner_user_id: int, platform: str, name: str
) -> BusinessAccess:
    with get_db() as conn:
        tenancy = TenancyRepository(conn)
        session = tenancy.get_owner_onboarding_session(
            user_id=owner_user_id, platform=platform
        )
        if session is None or session.step != OwnerOnboardingStep.BUSINESS_NAME:
            raise ValueError("business-name onboarding is not active")
        access = tenancy.create_business(owner_user_id=owner_user_id, name=name)
        tenancy.set_owner_control_workspace(
            user_id=owner_user_id,
            platform=platform,
            business_id=access.business.id,
        )
        tenancy.set_owner_onboarding_session(
            user_id=owner_user_id,
            platform=platform,
            step=OwnerOnboardingStep.ACTIVITY_DESCRIPTION,
            business_id=access.business.id,
        )
        return access


def complete_activity_onboarding(
    *,
    user_id: int,
    platform: str,
    activity_description: str,
    timezone_name: str,
) -> BusinessProfile:
    with get_db() as conn:
        tenancy = TenancyRepository(conn)
        session = tenancy.get_owner_onboarding_session(user_id=user_id, platform=platform)
        if (
            session is None
            or session.step != OwnerOnboardingStep.ACTIVITY_DESCRIPTION
            or session.business_id is None
        ):
            raise ValueError("activity onboarding is not active")
        actor = tenancy.resolve_context(user_id=user_id, business_id=session.business_id)
        profile = ActivityRepository(conn).upsert_profile(
            actor=actor,
            activity_description=activity_description,
            timezone_name=timezone_name,
        )
        tenancy.set_owner_control_workspace(
            user_id=user_id,
            platform=platform,
            business_id=session.business_id,
        )
        tenancy.clear_owner_onboarding_session(user_id=user_id, platform=platform)
        return profile

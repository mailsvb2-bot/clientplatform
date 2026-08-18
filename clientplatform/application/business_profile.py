from __future__ import annotations

from clientplatform.domain.business_profile import (
    BusinessProfileDetails,
    business_profile_review_lines,
    extract_explicit_business_profile_details,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.business_profile_details_repository import (
    BusinessProfileDetailsRepository,
    StoredBusinessProfileDetails,
)
from services.db import get_db, get_db_ro


def suggest_business_profile_details(description: str) -> BusinessProfileDetails:
    """Build conservative structured suggestions from explicit owner facts."""

    return extract_explicit_business_profile_details(description)


def save_business_profile_details(
    *,
    actor: TenantContext,
    details: BusinessProfileDetails,
) -> StoredBusinessProfileDetails:
    with get_db() as conn:
        return BusinessProfileDetailsRepository(conn).save(
            actor=actor,
            details=details,
            reset_confirmation=True,
        )


def get_business_profile_details(*, actor: TenantContext) -> StoredBusinessProfileDetails:
    with get_db_ro() as conn:
        return BusinessProfileDetailsRepository(conn).get(actor=actor)


def confirm_business_profile_details(*, actor: TenantContext) -> StoredBusinessProfileDetails:
    with get_db() as conn:
        return BusinessProfileDetailsRepository(conn).confirm(actor=actor)


def profile_review_text(*, activity_description: str, details: BusinessProfileDetails) -> str:
    facts = business_profile_review_lines(details)
    fact_block = "\n".join(f"• {line}" for line in facts)
    if fact_block:
        fact_block = f"\n\nЯ также выделил из Вашего текста:\n{fact_block}"
    return (
        "Я правильно понял?\n\n"
        f"{activity_description}{fact_block}\n\n"
        "Если что-то важное указано неверно или не хватает цены, города, контакта "
        "или правил записи — нажмите «Изменить». Иначе подтвердите и сразу выберите "
        "первый результат."
    )


__all__ = [
    "confirm_business_profile_details",
    "get_business_profile_details",
    "profile_review_text",
    "save_business_profile_details",
    "suggest_business_profile_details",
]

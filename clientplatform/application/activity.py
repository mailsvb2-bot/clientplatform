from __future__ import annotations

from clientplatform.domain.activity import (
    ActivityInvariantViolation,
    BusinessCapability,
    BusinessOffering,
    BusinessProfile,
    InviteClaim,
    IssuedCustomerInvite,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.postgres_safe_activity_repository import ActivityRepository
from services.db import get_db, get_db_ro

_REPOSITORY_INVITE_EXPIRED_ERROR = "customer invite has expired"
_CUSTOMER_INVITE_EXPIRED_MESSAGE = (
    "Срок действия ссылки истёк. Попросите специалиста отправить новую ссылку."
)


def save_business_profile(
    *,
    actor: TenantContext,
    activity_description: str,
    timezone_name: str,
) -> BusinessProfile:
    with get_db() as conn:
        return ActivityRepository(conn).upsert_profile(
            actor=actor,
            activity_description=activity_description,
            timezone_name=timezone_name,
        )


def get_business_profile(*, actor: TenantContext) -> BusinessProfile:
    with get_db_ro() as conn:
        return ActivityRepository(conn).get_profile(actor=actor)


def enable_business_capability(
    *,
    actor: TenantContext,
    connector_key: str,
    title: str | None = None,
) -> BusinessCapability:
    with get_db() as conn:
        return ActivityRepository(conn).enable_capability(
            actor=actor,
            connector_key=connector_key,
            title=title,
        )


def disable_business_capability(
    *,
    actor: TenantContext,
    connector_key: str,
) -> BusinessCapability:
    with get_db() as conn:
        return ActivityRepository(conn).disable_capability(
            actor=actor,
            connector_key=connector_key,
        )


def list_business_capabilities(
    *,
    actor: TenantContext,
    include_disabled: bool = False,
) -> list[BusinessCapability]:
    with get_db_ro() as conn:
        return ActivityRepository(conn).list_capabilities(
            actor=actor,
            include_disabled=include_disabled,
        )


def complete_business_profile(*, actor: TenantContext) -> BusinessProfile:
    with get_db() as conn:
        return ActivityRepository(conn).complete_profile(actor=actor)


def create_business_offering(
    *,
    actor: TenantContext,
    capability_id: str,
    title: str,
    description: str,
) -> BusinessOffering:
    with get_db() as conn:
        return ActivityRepository(conn).create_offering(
            actor=actor,
            capability_id=capability_id,
            title=title,
            description=description,
        )


def list_business_offerings(
    *,
    actor: TenantContext,
    capability_id: str,
) -> list[BusinessOffering]:
    with get_db_ro() as conn:
        return ActivityRepository(conn).list_offerings(
            actor=actor,
            capability_id=capability_id,
        )


def issue_customer_invite(*, actor: TenantContext, ttl_days: int = 7) -> IssuedCustomerInvite:
    with get_db() as conn:
        return ActivityRepository(conn).issue_customer_invite(actor=actor, ttl_days=ttl_days)


def claim_customer_invite(
    *,
    token: str,
    telegram_user_id: int,
    username: str | None,
    display_name: str | None,
) -> InviteClaim:
    expired_error: ActivityInvariantViolation | None = None
    with get_db() as conn:
        try:
            return ActivityRepository(conn).claim_customer_invite(
                token=token,
                telegram_user_id=telegram_user_id,
                username=username,
                display_name=display_name,
            )
        except ActivityInvariantViolation as exc:
            if str(exc) != _REPOSITORY_INVITE_EXPIRED_ERROR:
                raise
            # The repository has already transitioned the invite to `expired`.
            # Swallow only this specific signal until get_db() commits that
            # transition; re-raise a user-facing error after the transaction.
            expired_error = exc

    if expired_error is None:  # pragma: no cover - defensive invariant
        raise RuntimeError("customer invite expiration signal was lost")
    raise ActivityInvariantViolation(_CUSTOMER_INVITE_EXPIRED_MESSAGE) from expired_error

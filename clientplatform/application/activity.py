from __future__ import annotations

from clientplatform.domain.activity import (
    ActivityError,
    ActivityInvariantViolation,
    BusinessCapability,
    BusinessOffering,
    BusinessProfile,
    InviteClaim,
    IssuedCustomerInvite,
)
from clientplatform.domain.customers import CustomerPlatform, normalize_identity_subject
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.postgres_safe_activity_repository import ActivityRepository
from services.accounts.identity import resolve_account_for_identity
from services.db import get_db, get_db_ro

_REPOSITORY_INVITE_EXPIRED_ERROR = "customer invite has expired"
_CUSTOMER_INVITE_EXPIRED_MESSAGE = (
    "Срок действия ссылки истёк. Попросите специалиста отправить новую ссылку."
)
_CUSTOMER_INVITE_OWNER_MESSAGE = (
    "Эту ссылку нельзя использовать владельцу или сотруднику собственного бизнеса. "
    "Отправьте её другому клиенту."
)
_CUSTOMER_INVITE_INVALID_MESSAGE = (
    "Эта ссылка недействительна или больше не доступна. "
    "Попросите специалиста отправить новую ссылку."
)
_CUSTOMER_INVITE_ALREADY_USED_MESSAGE = (
    "Эта ссылка уже использована другим клиентом. "
    "Попросите специалиста отправить новую ссылку."
)
_CUSTOMER_INVITE_INACTIVE_MESSAGE = (
    "Эта ссылка больше не активна. Попросите специалиста отправить новую ссылку."
)
_CUSTOMER_INVITE_CONCURRENT_MESSAGE = (
    "Эту ссылку только что использовал другой клиент. "
    "Попросите специалиста отправить новую ссылку."
)
_CUSTOMER_INVITE_GENERIC_MESSAGE = (
    "Не удалось использовать эту ссылку. Попросите специалиста отправить новую ссылку."
)
_REPOSITORY_INVITE_PUBLIC_ERRORS = {
    "customer invite was not found": _CUSTOMER_INVITE_INVALID_MESSAGE,
    "invalid customer invite token": _CUSTOMER_INVITE_INVALID_MESSAGE,
    "customer invite has already been used": _CUSTOMER_INVITE_ALREADY_USED_MESSAGE,
    "customer invite is not active": _CUSTOMER_INVITE_INACTIVE_MESSAGE,
    "customer invite was claimed concurrently": _CUSTOMER_INVITE_CONCURRENT_MESSAGE,
}


def customer_invite_error_message(exc: Exception) -> str:
    """Return only stable, user-safe copy for invite claim failures."""

    message = str(exc).strip()
    if message in {_CUSTOMER_INVITE_EXPIRED_MESSAGE, _CUSTOMER_INVITE_OWNER_MESSAGE}:
        return message
    return _REPOSITORY_INVITE_PUBLIC_ERRORS.get(
        message,
        _CUSTOMER_INVITE_GENERIC_MESSAGE,
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
    idempotency_key: str | None = None,
) -> BusinessOffering:
    with get_db() as conn:
        return ActivityRepository(conn).create_offering(
            actor=actor,
            capability_id=capability_id,
            title=title,
            description=description,
            idempotency_key=idempotency_key,
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
    try:
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
    except (ActivityError, ValueError) as exc:
        raise ActivityInvariantViolation(customer_invite_error_message(exc)) from exc

    if expired_error is None:  # pragma: no cover - defensive invariant
        raise RuntimeError("customer invite expiration signal was lost")
    raise ActivityInvariantViolation(_CUSTOMER_INVITE_EXPIRED_MESSAGE) from expired_error



def extract_customer_invite_token(value: object) -> str | None:
    raw = " ".join(str(value or "").strip().split())
    payload = raw
    lowered = raw.casefold()
    if lowered.startswith("/start ") or lowered.startswith("start "):
        payload = raw.split(maxsplit=1)[1].strip()
    if not payload.casefold().startswith("cpj_"):
        return None
    return payload[4:].strip()

def claim_customer_invite_identity(
    *,
    token: str,
    platform: CustomerPlatform | str,
    external_subject: str,
    username: str | None,
    display_name: str | None,
    expected_business_id: str | None = None,
) -> InviteClaim:
    normalized_platform, normalized_subject = normalize_identity_subject(
        platform, external_subject
    )
    claiming_account_id = resolve_account_for_identity(
        normalized_platform.value,
        normalized_subject,
        username=username,
        display_name=display_name,
        allow_create=False,
    )
    expired_error: ActivityInvariantViolation | None = None
    try:
        with get_db() as conn:
            try:
                return ActivityRepository(conn).claim_customer_invite_identity(
                    token=token,
                    platform=normalized_platform,
                    external_subject=normalized_subject,
                    username=username,
                    display_name=display_name,
                    claiming_account_id=claiming_account_id,
                    expected_business_id=expected_business_id,
                )
            except ActivityInvariantViolation as exc:
                if str(exc) != _REPOSITORY_INVITE_EXPIRED_ERROR:
                    raise
                expired_error = exc
    except (ActivityError, ValueError) as exc:
        raise ActivityInvariantViolation(customer_invite_error_message(exc)) from exc

    if expired_error is None:  # pragma: no cover - defensive invariant
        raise RuntimeError("customer invite expiration signal was lost")
    raise ActivityInvariantViolation(_CUSTOMER_INVITE_EXPIRED_MESSAGE) from expired_error

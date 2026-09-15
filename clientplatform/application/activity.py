from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from clientplatform.domain.activity import (
    ActivityError,
    ActivityInvariantViolation,
    BusinessCapability,
    BusinessOffering,
    BusinessProfile,
    InviteClaim,
    IssuedCustomerInvite,
    OfferingStatus,
)
from clientplatform.domain.offering_process import BusinessOfferingProcess
from clientplatform.domain.customers import CustomerPlatform, normalize_identity_subject
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.offering_process_repository import OfferingProcessRepository
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


def _audit_offering_action(
    conn,
    *,
    actor: TenantContext,
    offering: BusinessOffering,
    action: str,
    detail: str,
) -> None:
    event_id = str(
        uuid5(
            NAMESPACE_URL,
            f"clientplatform:lifecycle:offering:{offering.business_id}:"
            f"{offering.id}:{action}:{offering.updated_at}",
        )
    )
    conn.execute(
        """
        INSERT INTO clientplatform_admin_audit_events(
            id, business_id, actor_user_id, action, subject_type,
            subject_id, detail, created_at
        ) VALUES(?, ?, ?, ?, 'offering', ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            event_id,
            offering.business_id,
            actor.user_id,
            action,
            offering.id,
            str(detail)[:1000],
            offering.updated_at,
        ),
    )


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
    now: str | None = None,
) -> BusinessOffering:
    with get_db() as conn:
        offering = ActivityRepository(conn).create_offering(
            actor=actor,
            capability_id=capability_id,
            title=title,
            description=description,
            idempotency_key=idempotency_key,
            now=now,
        )
        if offering.status == OfferingStatus.ACTIVE:
            OfferingProcessRepository(conn).ensure(
                business_id=offering.business_id,
                offering_id=offering.id,
                created_by_member_id=offering.created_by_member_id,
                now=now,
            )
        return offering


def rename_business_offering(
    *,
    actor: TenantContext,
    offering_id: str,
    title: str,
    now: str | None = None,
) -> BusinessOffering:
    with get_db() as conn:
        repository = ActivityRepository(conn)
        before = repository.get_offering(actor=actor, offering_id=offering_id)
        offering = repository.rename_offering(
            actor=actor,
            offering_id=offering_id,
            title=title,
            now=now,
        )
        if before.title != offering.title:
            _audit_offering_action(
                conn,
                actor=actor,
                offering=offering,
                action="offering_renamed",
                detail=f"{before.title} -> {offering.title}",
            )
        return offering


def archive_business_offering(
    *,
    actor: TenantContext,
    offering_id: str,
    now: str | None = None,
) -> BusinessOffering:
    with get_db() as conn:
        repository = ActivityRepository(conn)
        offering = repository.archive_offering(
            actor=actor, offering_id=offering_id, now=now
        )
        process_repository = OfferingProcessRepository(conn)
        process_repository.ensure(
            business_id=offering.business_id,
            offering_id=offering.id,
            created_by_member_id=offering.created_by_member_id,
            now=now,
        )
        process = process_repository.freeze(
            business_id=offering.business_id,
            offering_id=offering.id,
            now=now,
        )
        _audit_offering_action(
            conn,
            actor=actor,
            offering=offering,
            action="offering_archived",
            detail=(
                f"{offering.title} | process={process.state.value}"
                + (f" until {process.purge_after}" if process.purge_after else "")
            ),
        )
        return offering


def restore_business_offering(
    *,
    actor: TenantContext,
    offering_id: str,
    now: str | None = None,
) -> BusinessOffering:
    with get_db() as conn:
        repository = ActivityRepository(conn)
        offering = repository.restore_offering(
            actor=actor,
            offering_id=offering_id,
            now=now,
        )
        process, rebuilt = OfferingProcessRepository(conn).restore_or_rebuild(
            business_id=offering.business_id,
            offering_id=offering.id,
            created_by_member_id=offering.created_by_member_id,
            now=now,
        )
        _audit_offering_action(
            conn,
            actor=actor,
            offering=offering,
            action="offering_restored",
            detail=f"{offering.title} | process={'rebuilt' if rebuilt else 'restored'} | revision={process.revision}",
        )
        return offering


def get_business_offering_process(
    *,
    actor: TenantContext,
    offering_id: str,
) -> BusinessOfferingProcess:
    with get_db_ro() as conn:
        offering = ActivityRepository(conn).get_offering(actor=actor, offering_id=offering_id)
        return OfferingProcessRepository(conn).get(
            business_id=offering.business_id,
            offering_id=offering.id,
        )


def run_offering_process_retention_batch(
    *,
    now: str | None = None,
    limit: int = 100,
) -> int:
    with get_db() as conn:
        return OfferingProcessRepository(conn).purge_due(now=now, limit=limit)


def list_business_offerings(
    *,
    actor: TenantContext,
    capability_id: str,
    include_archived: bool = False,
) -> list[BusinessOffering]:
    with get_db_ro() as conn:
        return ActivityRepository(conn).list_offerings(
            actor=actor,
            capability_id=capability_id,
            include_archived=include_archived,
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

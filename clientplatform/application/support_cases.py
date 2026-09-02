from __future__ import annotations

"""Channel-neutral support-case orchestration for tenants and platform operators."""

from clientplatform.domain.support_cases import SupportCase, SupportCaseCategory, SupportCaseStatus
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.support_case_repository import SupportCaseRepository
from services.admin import is_platform_admin
from services.db import get_db, get_db_ro
from services.platform_support_access import (
    PlatformSupportSession,
    issue_support_session_in_transaction,
)


class PlatformSupportCasePermissionDenied(PermissionError):
    """The caller is not an explicitly configured platform operator."""


def _operator(user_id: int | None) -> int:
    if user_id is None or not is_platform_admin(user_id):
        raise PlatformSupportCasePermissionDenied("platform support queue access required")
    return int(user_id)


def create_support_case(
    *,
    actor: TenantContext,
    category: SupportCaseCategory | str,
    summary: object,
    idempotency_key: object,
) -> SupportCase:
    with get_db() as conn:
        return SupportCaseRepository(conn).create(
            actor=actor,
            category=category,
            summary=summary,
            idempotency_key=idempotency_key,
        )


def list_tenant_support_cases(*, actor: TenantContext, limit: int = 20) -> list[SupportCase]:
    with get_db_ro() as conn:
        return SupportCaseRepository(conn).list_for_tenant(actor=actor, limit=limit)


def list_platform_support_queue(
    user_id: int | None,
    *,
    limit: int = 50,
) -> list[SupportCase]:
    _operator(user_id)
    with get_db_ro() as conn:
        return SupportCaseRepository(conn).list_platform_queue(limit=limit)



def claim_platform_support_case(
    user_id: int | None,
    *,
    case_id: str,
    idempotency_key: object,
) -> SupportCase:
    operator_user_id = _operator(user_id)
    with get_db() as conn:
        return SupportCaseRepository(conn).claim_platform(
            operator_user_id=operator_user_id,
            case_id=case_id,
            idempotency_key=idempotency_key,
        )


def release_platform_support_case(
    user_id: int | None,
    *,
    case_id: str,
    idempotency_key: object,
) -> SupportCase:
    operator_user_id = _operator(user_id)
    with get_db() as conn:
        return SupportCaseRepository(conn).release_platform(
            operator_user_id=operator_user_id,
            case_id=case_id,
            idempotency_key=idempotency_key,
        )


def resolve_platform_support_case(
    user_id: int | None,
    *,
    case_id: str,
    idempotency_key: object,
) -> SupportCase:
    operator_user_id = _operator(user_id)
    with get_db() as conn:
        return SupportCaseRepository(conn).resolve_platform(
            operator_user_id=operator_user_id,
            case_id=case_id,
            idempotency_key=idempotency_key,
        )


def issue_support_session_for_case(
    user_id: int | None,
    *,
    case_id: str,
    reason: str,
    idempotency_key: str,
    ttl_seconds: int = 1800,
) -> PlatformSupportSession:
    """Atomically bridge one claimed case to the separate M6-002 capability boundary."""

    operator_user_id = _operator(user_id)
    with get_db() as conn:
        case = SupportCaseRepository(conn).require_claimed_for_platform_session(
            operator_user_id=operator_user_id,
            case_id=case_id,
        )
        return issue_support_session_in_transaction(
            operator_user_id,
            conn=conn,
            business_id=case.business_id,
            ticket_ref=f"support-case:{case.id}",
            reason=reason,
            idempotency_key=idempotency_key,
            ttl_seconds=ttl_seconds,
        )


__all__ = [
    "PlatformSupportCasePermissionDenied",
    "claim_platform_support_case",
    "create_support_case",
    "issue_support_session_for_case",
    "list_platform_support_queue",
    "list_tenant_support_cases",
    "release_platform_support_case",
    "resolve_platform_support_case",
]

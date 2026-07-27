from __future__ import annotations

from a1.domain.tenancy import Business, BusinessAccess, BusinessMember, PlatformRole, TenantContext
from a1.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db, get_db_ro


def create_business(*, owner_user_id: int, name: str) -> BusinessAccess:
    with get_db() as conn:
        return TenancyRepository(conn).create_business(owner_user_id=owner_user_id, name=name)


def resolve_tenant_context(*, user_id: int, business_id: str) -> TenantContext:
    with get_db_ro() as conn:
        return TenancyRepository(conn).resolve_context(user_id=user_id, business_id=business_id)


def list_accessible_businesses(*, user_id: int) -> list[BusinessAccess]:
    with get_db_ro() as conn:
        return TenancyRepository(conn).list_accessible_businesses(user_id=user_id)


def rename_business(*, actor: TenantContext, name: str) -> Business:
    with get_db() as conn:
        return TenancyRepository(conn).rename_business(actor=actor, name=name)


def grant_business_member(
    *,
    actor: TenantContext,
    user_id: int,
    role: PlatformRole | str,
) -> BusinessMember:
    with get_db() as conn:
        return TenancyRepository(conn).grant_member(actor=actor, user_id=user_id, role=role)


def revoke_business_member(*, actor: TenantContext, user_id: int) -> BusinessMember:
    with get_db() as conn:
        return TenancyRepository(conn).revoke_member(actor=actor, user_id=user_id)

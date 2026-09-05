from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from clientplatform.domain.tenancy import Business, BusinessAccess, BusinessMember, PlatformRole, TenantContext
from clientplatform.infrastructure import TenancyRepository
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


def set_owner_control_workspace(*, user_id: int, platform: str, business_id: str) -> str:
    with get_db() as conn:
        return TenancyRepository(conn).set_owner_control_workspace(
            user_id=user_id, platform=platform, business_id=business_id
        )


def get_owner_control_workspace(*, user_id: int, platform: str) -> str | None:
    with get_db_ro() as conn:
        return TenancyRepository(conn).get_owner_control_workspace(
            user_id=user_id, platform=platform
        )


def archive_business(*, actor: TenantContext) -> Business:
    with get_db() as conn:
        business = TenancyRepository(conn).archive_business(actor=actor)
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                f"clientplatform:lifecycle:business:{business.id}:archived",
            )
        )
        conn.execute(
            """
            INSERT INTO clientplatform_admin_audit_events(
                id, business_id, actor_user_id, action, subject_type,
                subject_id, detail, created_at
            ) VALUES(?, ?, ?, 'business_archived', 'business', ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                event_id,
                business.id,
                actor.user_id,
                business.id,
                business.name[:1000],
                business.updated_at,
            ),
        )
        return business


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

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.automation_policy import (
    AutomationPolicy,
    AutomationPolicyConflict,
    AutomationPolicyNotFound,
    AutomationPolicySpec,
    AutomationPolicyStatus,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied, normalize_uuid
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository


def _utc_now(value: datetime | str | None = None) -> str:
    if isinstance(value, datetime):
        current = value
    elif value is not None:
        raw = str(value).strip().replace("Z", "+00:00")
        current = datetime.fromisoformat(raw)
    else:
        current = datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("automation policy timestamp must be timezone-aware")
    return current.astimezone(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _policy_from_row(row: Any) -> AutomationPolicy:
    return AutomationPolicy(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        version=int(_value(row, "version", 2)),
        status=AutomationPolicyStatus(str(_value(row, "status", 3))),
        spec=AutomationPolicySpec.from_json(str(_value(row, "policy_json", 5))),
        policy_hash=str(_value(row, "policy_hash", 6)),
        created_by_member_id=str(_value(row, "created_by_member_id", 7)),
        approved_by_member_id=(
            None if _value(row, "approved_by_member_id", 8) is None else str(_value(row, "approved_by_member_id", 8))
        ),
        created_at=str(_value(row, "created_at", 9)),
        updated_at=str(_value(row, "updated_at", 10)),
        approved_at=None if _value(row, "approved_at", 11) is None else str(_value(row, "approved_at", 11)),
        revoked_at=None if _value(row, "revoked_at", 12) is None else str(_value(row, "revoked_at", 12)),
    )


_COLUMNS = """
    id, business_id, version, status, mode, policy_json, policy_hash,
    created_by_member_id, approved_by_member_id, created_at, updated_at,
    approved_at, revoked_at
""".strip()


class AutomationPolicyRepository:
    """Versioned canonical automation policy store scoped by active business membership."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _current(self, actor: TenantContext, *, manage: bool = False) -> TenantContext:
        current = self._tenancy.resolve_context(user_id=actor.user_id, business_id=actor.business_id)
        if manage:
            current.assert_can_manage_business()
        return current

    def _lock_business(self, business_id: str) -> None:
        cursor = self._conn.execute(
            "UPDATE businesses SET updated_at=updated_at WHERE id=? AND status='active'",
            (normalize_uuid(business_id, field_name="business_id"),),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AutomationPolicyConflict("automation_policy_business_inactive")

    def _audit(
        self,
        *,
        actor: TenantContext,
        action: str,
        policy: AutomationPolicy,
        detail: str,
        now: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO clientplatform_admin_audit_events(
                id, business_id, actor_user_id, action, subject_type,
                subject_id, detail, created_at
            ) VALUES(?, ?, ?, ?, 'automation_policy', ?, ?, ?)
            """,
            (
                str(uuid4()),
                actor.business_id,
                actor.user_id,
                action,
                policy.id,
                detail[:1000],
                now,
            ),
        )

    def create_draft(
        self,
        *,
        actor: TenantContext,
        spec: AutomationPolicySpec,
        expected_latest_version: int | None = None,
        policy_id: str | None = None,
        now: datetime | str | None = None,
    ) -> AutomationPolicy:
        current = self._current(actor, manage=True)
        timestamp = _utc_now(now)
        self._lock_business(current.business_id)
        row = self._conn.execute(
            "SELECT MAX(version) AS version FROM clientplatform_automation_policies WHERE business_id=?",
            (current.business_id,),
        ).fetchone()
        latest = 0 if row is None or _value(row, "version", 0) is None else int(_value(row, "version", 0))
        if expected_latest_version is not None and int(expected_latest_version) != latest:
            raise AutomationPolicyConflict("automation_policy_version_changed")
        version = latest + 1
        identifier = normalize_uuid(policy_id or str(uuid4()), field_name="policy_id")
        self._conn.execute(
            """
            INSERT INTO clientplatform_automation_policies(
                id, business_id, version, status, mode, policy_json, policy_hash,
                created_by_member_id, approved_by_member_id, created_at, updated_at,
                approved_at, revoked_at
            ) VALUES(?, ?, ?, 'draft', ?, ?, ?, ?, NULL, ?, ?, NULL, NULL)
            """,
            (
                identifier,
                current.business_id,
                version,
                spec.mode.value,
                spec.to_json(),
                spec.policy_hash,
                current.membership_id,
                timestamp,
                timestamp,
            ),
        )
        policy = self.get(actor=current, policy_id=identifier)
        self._audit(
            actor=current,
            action="automation_policy_draft_created",
            policy=policy,
            detail=f"version={version};hash={policy.policy_hash};mode={policy.spec.mode.value}",
            now=timestamp,
        )
        return policy

    def get(self, *, actor: TenantContext, policy_id: str) -> AutomationPolicy:
        current = self._current(actor)
        identifier = normalize_uuid(policy_id, field_name="policy_id")
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM clientplatform_automation_policies WHERE business_id=? AND id=? LIMIT 1",  # nosec B608
            (current.business_id, identifier),
        ).fetchone()
        if row is None:
            raise AutomationPolicyNotFound("automation_policy_not_found")
        return _policy_from_row(row)

    def latest(self, *, actor: TenantContext) -> AutomationPolicy | None:
        current = self._current(actor)
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM clientplatform_automation_policies "  # nosec B608
            "WHERE business_id=? ORDER BY version DESC LIMIT 1",
            (current.business_id,),
        ).fetchone()
        return None if row is None else _policy_from_row(row)

    def effective(self, *, actor: TenantContext, now: datetime | str | None = None) -> AutomationPolicy | None:
        current = self._current(actor)
        timestamp = _utc_now(now)
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM clientplatform_automation_policies "  # nosec B608
            "WHERE business_id=? AND status='approved' ORDER BY version DESC",
            (current.business_id,),
        ).fetchall()
        for row in rows:
            policy = _policy_from_row(row)
            if policy.is_effective(now=timestamp):
                return policy
        return None

    def approve(
        self,
        *,
        actor: TenantContext,
        policy_id: str,
        expected_policy_hash: str,
        now: datetime | str | None = None,
    ) -> AutomationPolicy:
        current = self._current(actor, manage=True)
        if current.role != PlatformRole.OWNER:
            raise TenantPermissionDenied("automation policy approval requires owner role")
        timestamp = _utc_now(now)
        self._lock_business(current.business_id)
        policy = self.get(actor=current, policy_id=policy_id)
        if policy.status != AutomationPolicyStatus.DRAFT:
            raise AutomationPolicyConflict("automation_policy_not_draft")
        if policy.policy_hash != str(expected_policy_hash or "").strip().lower():
            raise AutomationPolicyConflict("automation_policy_changed_before_approval")
        if datetime.fromisoformat(policy.spec.expires_at) <= datetime.fromisoformat(timestamp):
            raise AutomationPolicyConflict("automation_policy_expired_before_approval")
        self._conn.execute(
            """
            UPDATE clientplatform_automation_policies
            SET status='superseded', updated_at=?
            WHERE business_id=? AND status='approved' AND id<>?
            """,
            (timestamp, current.business_id, policy.id),
        )
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_automation_policies
            SET status='approved', approved_by_member_id=?, approved_at=?, updated_at=?
            WHERE business_id=? AND id=? AND status='draft' AND policy_hash=?
            """,
            (
                current.membership_id,
                timestamp,
                timestamp,
                current.business_id,
                policy.id,
                policy.policy_hash,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AutomationPolicyConflict("automation_policy_concurrent_approval")
        approved = self.get(actor=current, policy_id=policy.id)
        self._audit(
            actor=current,
            action="automation_policy_owner_approved",
            policy=approved,
            detail=f"version={approved.version};hash={approved.policy_hash};mode={approved.spec.mode.value}",
            now=timestamp,
        )
        return approved

    def revoke_effective(
        self,
        *,
        actor: TenantContext,
        now: datetime | str | None = None,
    ) -> AutomationPolicy | None:
        current = self._current(actor, manage=True)
        timestamp = _utc_now(now)
        self._lock_business(current.business_id)
        policy = self.effective(actor=current, now=timestamp)
        if policy is None:
            return None
        self._conn.execute(
            """
            UPDATE clientplatform_automation_policies
            SET status='revoked', revoked_at=?, updated_at=?
            WHERE business_id=? AND id=? AND status='approved'
            """,
            (timestamp, timestamp, current.business_id, policy.id),
        )
        revoked = self.get(actor=current, policy_id=policy.id)
        self._audit(
            actor=current,
            action="automation_policy_revoked",
            policy=revoked,
            detail=f"version={revoked.version};hash={revoked.policy_hash}",
            now=timestamp,
        )
        return revoked


__all__ = ["AutomationPolicyRepository"]

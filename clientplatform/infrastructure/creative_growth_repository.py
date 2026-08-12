from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import uuid4

from clientplatform.domain.creative_growth import (
    CreativeTrafficPlan,
    CreativeTrialArm,
    CreativeTrialStatus,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.core import DatabaseOperationDeadlineExceeded


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _trial_name(value: str) -> str:
    token = re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()
    if not token:
        raise ValueError("creative trial name must not be empty")
    if len(token) > 160:
        raise ValueError("creative trial name must be at most 160 characters")
    return token


class CreativeGrowthRepository:
    """Tenant persistence for controlled creative traffic plans.

    The plan is an application-side routing contract. It never claims to change
    an advertising provider's own delivery/budget algorithm.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _manager(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        return current

    def _viewer(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_promotion_analytics()
        return current

    def _binding_arm(
        self,
        *,
        business_id: str,
        publication_job_id: str,
        allocation_bps: int,
    ) -> CreativeTrialArm:
        job_id = normalize_uuid(publication_job_id, field_name="publication_job_id")
        row = self._conn.execute(
            """
            SELECT b.variant_id, j.promotion_campaign_id
            FROM creative_variant_bindings b
            JOIN ad_publication_jobs j
              ON j.id=b.publication_job_id AND j.business_id=b.business_id
            WHERE b.business_id=? AND b.publication_job_id=?
            LIMIT 1
            """,
            (business_id, job_id),
        ).fetchone()
        if row is None:
            raise LookupError("creative growth variant binding was not found")
        return CreativeTrialArm(
            variant_id=str(_value(row, "variant_id", 0)),
            publication_job_id=job_id,
            allocation_bps=int(allocation_bps),
            promotion_campaign_id=str(_value(row, "promotion_campaign_id", 1) or ""),
        ).normalized()

    def create(
        self,
        *,
        actor: TenantContext,
        name: str,
        allocations: Iterable[tuple[str, int]],
    ) -> CreativeTrafficPlan:
        current = self._manager(actor)
        arms = tuple(
            self._binding_arm(
                business_id=current.business_id,
                publication_job_id=publication_job_id,
                allocation_bps=allocation_bps,
            )
            for publication_job_id, allocation_bps in allocations
        )
        trial_id = str(uuid4())
        plan = CreativeTrafficPlan(
            trial_id=trial_id,
            business_id=current.business_id,
            status=CreativeTrialStatus.DRAFT,
            revision=1,
            arms=arms,
        ).normalized()
        now = _iso_now()
        self._conn.execute("SAVEPOINT creative_growth_create")
        try:
            self._conn.execute(
                """
                INSERT INTO creative_growth_trials(
                    id, business_id, name, status, revision,
                    created_by_member_id, created_at, updated_at
                ) VALUES(?, ?, ?, 'draft', 1, ?, ?, ?)
                """,
                (
                    plan.trial_id,
                    current.business_id,
                    _trial_name(name),
                    current.membership_id,
                    now,
                    now,
                ),
            )
            for position, arm in enumerate(plan.arms):
                self._conn.execute(
                    """
                    INSERT INTO creative_growth_trial_variants(
                        trial_id, business_id, variant_id, publication_job_id,
                        position, allocation_bps, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan.trial_id,
                        current.business_id,
                        arm.variant_id,
                        arm.publication_job_id,
                        position,
                        arm.allocation_bps,
                        now,
                        now,
                    ),
                )
        except (sqlite3.Error, DatabaseOperationDeadlineExceeded):
            self._conn.execute("ROLLBACK TO SAVEPOINT creative_growth_create")
            self._conn.execute("RELEASE SAVEPOINT creative_growth_create")
            raise
        self._conn.execute("RELEASE SAVEPOINT creative_growth_create")
        return self.get(actor=current, trial_id=plan.trial_id)

    def get(self, *, actor: TenantContext, trial_id: str) -> CreativeTrafficPlan:
        current = self._viewer(actor)
        normalized_trial = normalize_uuid(trial_id, field_name="trial_id")
        trial = self._conn.execute(
            """
            SELECT status, revision
            FROM creative_growth_trials
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (normalized_trial, current.business_id),
        ).fetchone()
        if trial is None:
            raise LookupError("creative growth trial was not found")
        rows = self._conn.execute(
            """
            SELECT v.variant_id, v.publication_job_id, v.allocation_bps,
                   j.promotion_campaign_id
            FROM creative_growth_trial_variants v
            JOIN ad_publication_jobs j
              ON j.id=v.publication_job_id AND j.business_id=v.business_id
            WHERE v.trial_id=? AND v.business_id=?
            ORDER BY v.position
            """,
            (normalized_trial, current.business_id),
        ).fetchall()
        return CreativeTrafficPlan(
            trial_id=normalized_trial,
            business_id=current.business_id,
            status=CreativeTrialStatus(str(_value(trial, "status", 0))),
            revision=int(_value(trial, "revision", 1)),
            arms=tuple(
                CreativeTrialArm(
                    variant_id=str(_value(row, "variant_id", 0)),
                    publication_job_id=str(_value(row, "publication_job_id", 1)),
                    allocation_bps=int(_value(row, "allocation_bps", 2)),
                    promotion_campaign_id=str(_value(row, "promotion_campaign_id", 3) or ""),
                )
                for row in rows
            ),
        ).normalized()

    def list(self, *, actor: TenantContext) -> tuple[CreativeTrafficPlan, ...]:
        current = self._viewer(actor)
        rows = self._conn.execute(
            """
            SELECT id FROM creative_growth_trials
            WHERE business_id=?
            ORDER BY updated_at DESC, id
            """,
            (current.business_id,),
        ).fetchall()
        return tuple(
            self.get(actor=current, trial_id=str(_value(row, "id", 0)))
            for row in rows
        )

    def replace_allocations(
        self,
        *,
        actor: TenantContext,
        trial_id: str,
        allocations: Iterable[tuple[str, int]],
    ) -> CreativeTrafficPlan:
        current = self._manager(actor)
        existing = self.get(actor=current, trial_id=trial_id)
        if existing.status == CreativeTrialStatus.COMPLETED:
            raise ValueError("completed creative trial cannot be changed")
        requested = {
            normalize_uuid(job_id, field_name="publication_job_id"): int(allocation)
            for job_id, allocation in allocations
        }
        if set(requested) != {arm.publication_job_id for arm in existing.arms}:
            raise ValueError("creative trial allocation update must preserve its variants")
        probe = CreativeTrafficPlan(
            trial_id=existing.trial_id,
            business_id=existing.business_id,
            status=existing.status,
            revision=existing.revision + 1,
            arms=tuple(
                CreativeTrialArm(
                    variant_id=arm.variant_id,
                    publication_job_id=arm.publication_job_id,
                    allocation_bps=requested[arm.publication_job_id],
                    promotion_campaign_id=arm.promotion_campaign_id,
                )
                for arm in existing.arms
            ),
        ).normalized()
        now = _iso_now()
        self._conn.execute("SAVEPOINT creative_growth_reallocate")
        try:
            for arm in probe.arms:
                self._conn.execute(
                    """
                    UPDATE creative_growth_trial_variants
                    SET allocation_bps=?, updated_at=?
                    WHERE trial_id=? AND business_id=? AND publication_job_id=?
                    """,
                    (
                        arm.allocation_bps,
                        now,
                        probe.trial_id,
                        current.business_id,
                        arm.publication_job_id,
                    ),
                )
            self._conn.execute(
                """
                UPDATE creative_growth_trials
                SET revision=?, updated_at=?
                WHERE id=? AND business_id=?
                """,
                (probe.revision, now, probe.trial_id, current.business_id),
            )
        except (sqlite3.Error, DatabaseOperationDeadlineExceeded):
            self._conn.execute("ROLLBACK TO SAVEPOINT creative_growth_reallocate")
            self._conn.execute("RELEASE SAVEPOINT creative_growth_reallocate")
            raise
        self._conn.execute("RELEASE SAVEPOINT creative_growth_reallocate")
        return self.get(actor=current, trial_id=probe.trial_id)

    def set_status(
        self,
        *,
        actor: TenantContext,
        trial_id: str,
        status: CreativeTrialStatus,
    ) -> CreativeTrafficPlan:
        current = self._manager(actor)
        existing = self.get(actor=current, trial_id=trial_id)
        target = CreativeTrialStatus(status)
        if existing.status == CreativeTrialStatus.COMPLETED and target != existing.status:
            raise ValueError("completed creative trial cannot be reopened")
        if target == existing.status:
            return existing
        if target == CreativeTrialStatus.RUNNING:
            rows = self._conn.execute(
                """
                SELECT b.publication_job_id, b.status, a.source
                FROM creative_growth_trial_variants v
                JOIN creative_variant_bindings b
                  ON b.publication_job_id=v.publication_job_id
                 AND b.business_id=v.business_id
                LEFT JOIN ad_publication_assets a
                  ON a.publication_job_id=v.publication_job_id
                 AND a.business_id=v.business_id
                WHERE v.trial_id=? AND v.business_id=?
                """,
                (existing.trial_id, current.business_id),
            ).fetchall()
            if len(rows) != len(existing.arms) or any(
                str(_value(row, "status", 1)) != "attached"
                or str(_value(row, "source", 2) or "") != "generated"
                for row in rows
            ):
                raise ValueError("creative trial requires attached generated variants before running")
        next_revision = existing.revision + 1
        self._conn.execute(
            """
            UPDATE creative_growth_trials
            SET status=?, revision=?, updated_at=?
            WHERE id=? AND business_id=?
            """,
            (
                target.value,
                next_revision,
                _iso_now(),
                existing.trial_id,
                current.business_id,
            ),
        )
        return self.get(actor=current, trial_id=existing.trial_id)

    def assign(
        self,
        *,
        actor: TenantContext,
        trial_id: str,
        subject_key: str,
    ) -> CreativeTrialArm:
        return self.get(actor=actor, trial_id=trial_id).assign(subject_key)


__all__ = ["CreativeGrowthRepository"]

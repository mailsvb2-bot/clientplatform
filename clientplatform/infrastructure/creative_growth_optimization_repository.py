from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from clientplatform.domain.creative_growth import (
    CreativeTrafficPlan,
    CreativeTrialArm,
    CreativeTrialStatus,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.creative_growth_repository import CreativeGrowthRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.core import DatabaseOperationDeadlineExceeded


class StaleCreativeOptimizationError(RuntimeError):
    """Raised when an optimization decision no longer matches the current trial."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CreativeGrowthOptimizationRepository:
    """Atomically apply one already-reviewed allocation decision."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._tenancy = TenancyRepository(conn)
        self._growth = CreativeGrowthRepository(conn)

    def apply(
        self,
        *,
        actor: TenantContext,
        trial_id: str,
        expected_revision: int,
        allocations: Iterable[tuple[str, str, int, int]],
    ) -> CreativeTrafficPlan:
        """CAS apply `(variant_id, job_id, current_bps, proposed_bps)` rows."""

        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        existing = self._growth.get(actor=current, trial_id=trial_id)
        expected = int(expected_revision)
        if existing.status != CreativeTrialStatus.RUNNING:
            raise StaleCreativeOptimizationError("creative trial is no longer running")
        if existing.revision != expected:
            raise StaleCreativeOptimizationError("creative trial changed after recommendation")

        requested = {
            str(job_id): (str(variant_id), int(current_bps), int(proposed_bps))
            for variant_id, job_id, current_bps, proposed_bps in allocations
        }
        if set(requested) != {arm.publication_job_id for arm in existing.arms}:
            raise StaleCreativeOptimizationError("creative trial variants changed after recommendation")
        if any(
            requested[arm.publication_job_id][0] != arm.variant_id
            or requested[arm.publication_job_id][1] != arm.allocation_bps
            for arm in existing.arms
        ):
            raise StaleCreativeOptimizationError("creative trial allocation changed after recommendation")

        probe = CreativeTrafficPlan(
            trial_id=existing.trial_id,
            business_id=existing.business_id,
            status=existing.status,
            revision=existing.revision + 1,
            arms=tuple(
                CreativeTrialArm(
                    variant_id=arm.variant_id,
                    publication_job_id=arm.publication_job_id,
                    allocation_bps=requested[arm.publication_job_id][2],
                    promotion_campaign_id=arm.promotion_campaign_id,
                    promotion_source_token=arm.promotion_source_token,
                )
                for arm in existing.arms
            ),
        ).normalized()

        now = _iso_now()
        self._conn.execute("SAVEPOINT creative_growth_optimizer_apply")
        try:
            cursor = self._conn.execute(
                """
                UPDATE creative_growth_trials
                SET revision=?, updated_at=?
                WHERE id=? AND business_id=? AND revision=? AND status='running'
                """,
                (
                    probe.revision,
                    now,
                    probe.trial_id,
                    current.business_id,
                    expected,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise StaleCreativeOptimizationError(
                    "creative trial changed while applying recommendation"
                )
            for arm in probe.arms:
                update = self._conn.execute(
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
                if int(getattr(update, "rowcount", 0) or 0) != 1:
                    raise StaleCreativeOptimizationError(
                        "creative trial variant changed while applying recommendation"
                    )
        except (
            sqlite3.Error,
            DatabaseOperationDeadlineExceeded,
            StaleCreativeOptimizationError,
        ):
            self._conn.execute("ROLLBACK TO SAVEPOINT creative_growth_optimizer_apply")
            self._conn.execute("RELEASE SAVEPOINT creative_growth_optimizer_apply")
            raise
        self._conn.execute("RELEASE SAVEPOINT creative_growth_optimizer_apply")
        return self._growth.get(actor=current, trial_id=probe.trial_id)


__all__ = [
    "CreativeGrowthOptimizationRepository",
    "StaleCreativeOptimizationError",
]

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from clientplatform.domain.connections import (
    ClaimedDispatch,
    Dispatch,
    DispatchLeaseLost,
    DispatchStatus,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.dispatch_outbox import (
    DispatchOutboxRepository as _BaseDispatchOutboxRepository,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class DispatchOutboxRepository(_BaseDispatchOutboxRepository):
    """Canonical outbox policy for immediate materialization and safe retries."""

    def __init__(self, conn: Any):
        super().__init__(conn)

    def materialize(
        self,
        *,
        actor: TenantContext,
        logical_delivery_id: str,
        connection_id: str,
        customer_identity_id: str,
        now: str | None = None,
    ) -> Dispatch:
        """Create an immediately due dispatch.

        ``now`` is an injectable audit timestamp. It must not silently become a
        scheduling API: future delivery will use an explicit schedule field.
        This also makes a clock-skewed producer unable to stall fresh work.
        """

        dispatch = super().materialize(
            actor=actor,
            logical_delivery_id=logical_delivery_id,
            connection_id=connection_id,
            customer_identity_id=customer_identity_id,
            now=now,
        )
        wall_clock = _utc_now().isoformat()
        if (
            dispatch.status == DispatchStatus.PENDING
            and dispatch.available_at > wall_clock
        ):
            self._conn.execute(
                """
                UPDATE delivery_dispatch_outbox
                SET available_at=?
                WHERE id=? AND business_id=? AND status='pending'
                  AND available_at>?
                """,
                (
                    wall_clock,
                    dispatch.id,
                    dispatch.business_id,
                    wall_clock,
                ),
            )
            return self._get_dispatch_system(
                business_id=dispatch.business_id,
                dispatch_id=dispatch.id,
            )
        return dispatch

    def reschedule(
        self,
        item: ClaimedDispatch,
        *,
        error: str,
        max_attempts: int = 8,
        now: datetime | None = None,
    ) -> Dispatch:
        failed_at = (now or _utc_now()).replace(microsecond=0)
        attempts = item.dispatch.attempts + 1
        terminal = attempts >= max(1, int(max_attempts))
        delay_seconds = min(5 * (2 ** max(0, attempts - 1)), 900)
        available_at = (failed_at + timedelta(seconds=delay_seconds)).isoformat()
        status = DispatchStatus.DEAD if terminal else DispatchStatus.RETRY
        error_text = str(error or "dispatch_failed").strip()[:1000]
        cursor = self._conn.execute(
            """
            UPDATE delivery_dispatch_outbox
            SET status=?, attempts=?, available_at=?, updated_at=?,
                locked_at=NULL, lock_token=NULL, last_error=?, dead_at=?
            WHERE id=? AND business_id=? AND status='sending' AND lock_token=?
            """,
            (
                status.value,
                attempts,
                available_at,
                failed_at.isoformat(),
                error_text,
                failed_at.isoformat() if terminal else None,
                item.dispatch.id,
                item.dispatch.business_id,
                item.dispatch.lock_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise DispatchLeaseLost("dispatch lease was lost before retry")

        if terminal:
            self._conn.execute(
                """
                UPDATE connections
                SET status='attention', last_error_at=?, last_error_code=?,
                    updated_at=?
                WHERE id=? AND business_id=? AND status='active'
                """,
                (
                    failed_at.isoformat(),
                    error_text[:240],
                    failed_at.isoformat(),
                    item.dispatch.connection_id,
                    item.dispatch.business_id,
                ),
            )
            self._conn.execute(
                """
                UPDATE lesson_deliveries
                SET status='failed', attempts=?, failed_at=?, last_error=?,
                    updated_at=?
                WHERE id=? AND business_id=? AND status IN ('pending','failed')
                """,
                (
                    attempts,
                    failed_at.isoformat(),
                    error_text,
                    failed_at.isoformat(),
                    item.dispatch.logical_delivery_id,
                    item.dispatch.business_id,
                ),
            )
        else:
            self._conn.execute(
                """
                UPDATE connections
                SET last_error_at=?, last_error_code=?, updated_at=?
                WHERE id=? AND business_id=? AND status='active'
                """,
                (
                    failed_at.isoformat(),
                    error_text[:240],
                    failed_at.isoformat(),
                    item.dispatch.connection_id,
                    item.dispatch.business_id,
                ),
            )

        return self._get_dispatch_system(
            business_id=item.dispatch.business_id,
            dispatch_id=item.dispatch.id,
        )

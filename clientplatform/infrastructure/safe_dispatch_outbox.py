from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from clientplatform.domain.connections import (
    ClaimedDispatch,
    ConnectionPlatform,
    Dispatch,
    DispatchLeaseLost,
    DispatchStatus,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.dispatch_outbox import (
    DispatchOutboxRepository as _BaseDispatchOutboxRepository,
)


_MAX_PROVIDER_BOUNDARY_MARKER = "max_provider_call_started_non_idempotent"
_MAX_AMBIGUOUS_ERROR = "max_delivery_outcome_ambiguous_manual_reconciliation_required"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _row_value(row: Any, key: str, index: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[index]


def mark_non_replay_safe_dispatch_boundary(conn: Any, item: ClaimedDispatch) -> None:
    """Durably mark a MAX dispatch immediately before crossing the provider boundary.

    MAX does not expose a documented provider idempotency key for message creation.
    Once this marker is persisted, a crash cannot turn the stale lease into an
    automatic replay. The next claimant quarantines it as ambiguous instead.
    """

    if item.dispatch.platform != ConnectionPlatform.MAX:
        return
    timestamp = _utc_now().isoformat()
    cursor = conn.execute(
        """
        UPDATE delivery_dispatch_outbox
        SET last_error=?, updated_at=?
        WHERE id=? AND business_id=? AND platform='max'
          AND status='sending' AND lock_token=?
        """,
        (
            _MAX_PROVIDER_BOUNDARY_MARKER,
            timestamp,
            item.dispatch.id,
            item.dispatch.business_id,
            item.dispatch.lock_token,
        ),
    )
    if int(getattr(cursor, "rowcount", 0) or 0) != 1:
        raise DispatchLeaseLost("MAX dispatch lease was lost before provider boundary")


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

    def _quarantine_stale_max_boundaries(
        self,
        *,
        lock_ttl_seconds: int,
        now: datetime | None = None,
    ) -> int:
        current = (now or _utc_now()).replace(microsecond=0)
        current_iso = current.isoformat()
        stale_before = (
            current - timedelta(seconds=max(1, int(lock_ttl_seconds)))
        ).isoformat()
        rows = self._conn.execute(
            """
            SELECT id,business_id,logical_delivery_id,connection_id,attempts
            FROM delivery_dispatch_outbox
            WHERE platform='max' AND status='sending'
              AND locked_at IS NOT NULL AND locked_at<=?
              AND last_error=?
            ORDER BY locked_at,id
            """,
            (stale_before, _MAX_PROVIDER_BOUNDARY_MARKER),
        ).fetchall()
        quarantined = 0
        for row in rows:
            dispatch_id = str(_row_value(row, "id", 0))
            business_id = str(_row_value(row, "business_id", 1))
            logical_delivery_id = str(_row_value(row, "logical_delivery_id", 2))
            connection_id = str(_row_value(row, "connection_id", 3))
            attempts = int(_row_value(row, "attempts", 4) or 0) + 1
            cursor = self._conn.execute(
                """
                UPDATE delivery_dispatch_outbox
                SET status='dead', attempts=?, updated_at=?, dead_at=?,
                    locked_at=NULL, lock_token=NULL, last_error=?
                WHERE id=? AND business_id=? AND platform='max'
                  AND status='sending' AND locked_at<=? AND last_error=?
                """,
                (
                    attempts,
                    current_iso,
                    current_iso,
                    _MAX_AMBIGUOUS_ERROR,
                    dispatch_id,
                    business_id,
                    stale_before,
                    _MAX_PROVIDER_BOUNDARY_MARKER,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                continue
            quarantined += 1
            self._conn.execute(
                """
                UPDATE lesson_deliveries
                SET status='failed', attempts=?, failed_at=?, last_error=?,
                    updated_at=?
                WHERE id=? AND business_id=? AND status IN ('pending','failed')
                """,
                (
                    attempts,
                    current_iso,
                    _MAX_AMBIGUOUS_ERROR,
                    current_iso,
                    logical_delivery_id,
                    business_id,
                ),
            )
            self._conn.execute(
                """
                UPDATE connections
                SET status='attention', last_error_at=?, last_error_code=?,
                    updated_at=?
                WHERE id=? AND business_id=? AND status='active'
                """,
                (
                    current_iso,
                    _MAX_AMBIGUOUS_ERROR[:240],
                    current_iso,
                    connection_id,
                    business_id,
                ),
            )
        return quarantined

    def claim_due(
        self,
        *,
        limit: int = 10,
        lock_ttl_seconds: int = 900,
        now: datetime | None = None,
    ) -> list[ClaimedDispatch]:
        self._quarantine_stale_max_boundaries(
            lock_ttl_seconds=lock_ttl_seconds,
            now=now,
        )
        return _BaseDispatchOutboxRepository.claim_due(
            self,
            limit=limit,
            lock_ttl_seconds=lock_ttl_seconds,
            now=now,
        )

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


__all__ = ["DispatchOutboxRepository", "mark_non_replay_safe_dispatch_boundary"]

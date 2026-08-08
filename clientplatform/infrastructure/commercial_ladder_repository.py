from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.commercial_ladder import (
    CommercialLadderStep,
    CommercialOfferCandidate,
    CommercialStepKind,
    eligible_offer_candidates,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _step_from_row(row: Any) -> CommercialLadderStep:
    offering = _value(row, "offering_id", 6)
    return CommercialLadderStep(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        ladder_id=str(_value(row, "ladder_id", 2)),
        position=int(_value(row, "position", 3)),
        kind=CommercialStepKind(str(_value(row, "kind", 4))),
        title=str(_value(row, "title", 5)),
        offering_id=None if offering is None else str(offering),
        min_evidence_score=float(_value(row, "min_evidence_score", 7)),
        requires_human_approval=bool(_value(row, "requires_human_approval", 8)),
    )


class CommercialLadderRepository:
    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _current(self, actor: TenantContext, *, manage: bool) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id, business_id=actor.business_id
        )
        if manage:
            current.assert_can_manage_business()
        else:
            current.assert_can_view_promotion_analytics()
        return current

    def _require_active_ladder(self, *, business_id: str, ladder_id: str) -> None:
        if self._conn.execute(
            """
            SELECT 1 FROM commercial_ladders
            WHERE id=? AND business_id=? AND status='active' LIMIT 1
            """,
            (ladder_id, business_id),
        ).fetchone() is None:
            raise ValueError("commercial ladder was not found in the active business")

    def create_ladder(
        self,
        *,
        actor: TenantContext,
        name: str,
        now: str | None = None,
    ) -> str:
        current = self._current(actor, manage=True)
        normalized = re.sub(r"\s+", " ", str(name or "")).strip()
        if not normalized or len(normalized) > 160:
            raise ValueError("commercial ladder name must be 1..160 characters")
        ladder_id = str(uuid4())
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            INSERT INTO commercial_ladders(
                id, business_id, name, status, created_by_member_id,
                created_at, updated_at
            ) VALUES(?,?,?,'active',?,?,?)
            """,
            (
                ladder_id,
                current.business_id,
                normalized,
                current.membership_id,
                timestamp,
                timestamp,
            ),
        )
        return ladder_id

    def add_step(
        self,
        *,
        actor: TenantContext,
        ladder_id: str,
        position: int,
        kind: CommercialStepKind | str,
        title: str,
        offering_id: str | None = None,
        min_evidence_score: float = 0.0,
        requires_human_approval: bool = True,
        now: str | None = None,
    ) -> CommercialLadderStep:
        current = self._current(actor, manage=True)
        ladder = normalize_uuid(ladder_id, field_name="ladder_id")
        self._require_active_ladder(
            business_id=current.business_id, ladder_id=ladder
        )
        selected_kind = (
            kind if isinstance(kind, CommercialStepKind) else CommercialStepKind(str(kind))
        )
        normalized_offering = None
        if offering_id is not None:
            normalized_offering = normalize_uuid(offering_id, field_name="offering_id")
            if self._conn.execute(
                """
                SELECT 1 FROM business_offerings
                WHERE id=? AND business_id=? AND status='active' LIMIT 1
                """,
                (normalized_offering, current.business_id),
            ).fetchone() is None:
                raise ValueError("offering was not found in the active business")
        step_id = str(uuid4())
        validated = CommercialLadderStep(
            id=step_id,
            business_id=current.business_id,
            ladder_id=ladder,
            position=position,
            kind=selected_kind,
            title=title,
            offering_id=normalized_offering,
            min_evidence_score=min_evidence_score,
            requires_human_approval=requires_human_approval,
        )
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            INSERT INTO commercial_ladder_steps(
                id, business_id, ladder_id, position, kind, title, offering_id,
                min_evidence_score, requires_human_approval, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                step_id,
                current.business_id,
                ladder,
                validated.position,
                validated.kind.value,
                validated.title,
                validated.offering_id,
                validated.min_evidence_score,
                1 if validated.requires_human_approval else 0,
                timestamp,
                timestamp,
            ),
        )
        return self.get_step(actor=current, step_id=step_id)

    def get_step(
        self, *, actor: TenantContext, step_id: str
    ) -> CommercialLadderStep:
        current = self._current(actor, manage=False)
        normalized = normalize_uuid(step_id, field_name="ladder_step_id")
        row = self._conn.execute(
            """
            SELECT id, business_id, ladder_id, position, kind, title,
                   offering_id, min_evidence_score, requires_human_approval
            FROM commercial_ladder_steps
            WHERE id=? AND business_id=? LIMIT 1
            """,
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise ValueError("commercial ladder step was not found in the active business")
        return _step_from_row(row)

    def list_steps(
        self, *, actor: TenantContext, ladder_id: str
    ) -> tuple[CommercialLadderStep, ...]:
        current = self._current(actor, manage=False)
        ladder = normalize_uuid(ladder_id, field_name="ladder_id")
        self._require_active_ladder(
            business_id=current.business_id, ladder_id=ladder
        )
        rows = self._conn.execute(
            """
            SELECT id, business_id, ladder_id, position, kind, title,
                   offering_id, min_evidence_score, requires_human_approval
            FROM commercial_ladder_steps
            WHERE business_id=? AND ladder_id=?
            ORDER BY position, id
            """,
            (current.business_id, ladder),
        ).fetchall()
        return tuple(_step_from_row(row) for row in rows)

    def candidates(
        self,
        *,
        actor: TenantContext,
        ladder_id: str,
        evidence_score: float,
        completed_step_ids: set[str] | frozenset[str] = frozenset(),
    ) -> tuple[CommercialOfferCandidate, ...]:
        steps = self.list_steps(actor=actor, ladder_id=ladder_id)
        return eligible_offer_candidates(
            steps,
            evidence_score=evidence_score,
            completed_step_ids=completed_step_ids,
        )

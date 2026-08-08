from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.sales import SalesLeadStage
from clientplatform.domain.sales_state_machine import (
    SalesConversationEvent,
    SalesConversationState,
    SalesTransition,
    coarse_sales_stage,
    reduce_sales_conversation,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.sales_repository import SalesRepository


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _transition_dedupe_key(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 200:
        raise ValueError("dedupe_key must be 1..200 characters")
    return f"conversation_transition:{normalized}"


class SalesStateRepository:
    """Replay-safe, compare-and-swap conversation state over the sales event log."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._sales = SalesRepository(conn)

    def current_state(
        self, *, actor: TenantContext, lead_id: str
    ) -> SalesConversationState:
        lead = self._sales.get_lead(actor=actor, lead_id=lead_id)
        row = self._conn.execute(
            """
            SELECT state
            FROM clientplatform_sales_conversation_state
            WHERE lead_id=? AND business_id=?
            LIMIT 1
            """,
            (lead.id, lead.business_id),
        ).fetchone()
        if row is None:
            return SalesConversationState.DISCOVERED
        return SalesConversationState(str(_value(row, "state", 0)))

    @staticmethod
    def _transition_from_payload(payload_raw: object) -> SalesTransition | None:
        try:
            payload = json.loads(str(payload_raw or "{}"))
            return SalesTransition(
                previous=SalesConversationState(str(payload["from"])),
                event=SalesConversationEvent(str(payload["event"])),
                current=SalesConversationState(str(payload["to"])),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def apply(
        self,
        *,
        actor: TenantContext,
        lead_id: str,
        event: SalesConversationEvent | str,
        dedupe_key: str,
        metadata: dict[str, object] | None = None,
        now: str | None = None,
    ) -> tuple[SalesTransition, bool]:
        lead = self._sales.get_lead(actor=actor, lead_id=lead_id)
        transition_dedupe = _transition_dedupe_key(dedupe_key)

        # Provider/webhook replay must be detected before reducing the current
        # state. Otherwise the same already-consumed signal could look like an
        # invalid transition after the first application.
        replay = self._conn.execute(
            """
            SELECT payload_json
            FROM clientplatform_sales_events
            WHERE business_id=? AND lead_id=? AND dedupe_key=?
              AND event_type='conversation_transition'
            LIMIT 1
            """,
            (lead.business_id, lead.id, transition_dedupe),
        ).fetchone()
        if replay is not None:
            transition = self._transition_from_payload(
                _value(replay, "payload_json", 0)
            )
            if transition is None:
                raise RuntimeError("stored sales transition evidence is invalid")
            return transition, False

        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            INSERT INTO clientplatform_sales_conversation_state(
                lead_id, business_id, state, version, updated_at
            ) VALUES(?,?,'discovered',1,?)
            ON CONFLICT(lead_id, business_id) DO NOTHING
            """,
            (lead.id, lead.business_id, timestamp),
        )
        state_row = self._conn.execute(
            """
            SELECT state, version
            FROM clientplatform_sales_conversation_state
            WHERE lead_id=? AND business_id=?
            LIMIT 1
            """,
            (lead.id, lead.business_id),
        ).fetchone()
        if state_row is None:
            raise RuntimeError("sales conversation state initialization failed")
        state = SalesConversationState(str(_value(state_row, "state", 0)))
        version = int(_value(state_row, "version", 1))
        transition = reduce_sales_conversation(state, event)
        payload = {
            "from": transition.previous.value,
            "event": transition.event.value,
            "to": transition.current.value,
            "metadata": dict(metadata or {}),
        }
        inserted = self._sales.record_event(
            actor=actor,
            lead_id=lead.id,
            event_type="conversation_transition",
            dedupe_key=transition_dedupe,
            payload=payload,
            now=now,
        )
        if not inserted:
            # A concurrent identical provider event won the unique dedupe key.
            replay = self._conn.execute(
                """
                SELECT payload_json
                FROM clientplatform_sales_events
                WHERE business_id=? AND lead_id=? AND dedupe_key=? LIMIT 1
                """,
                (lead.business_id, lead.id, transition_dedupe),
            ).fetchone()
            recovered = (
                None
                if replay is None
                else self._transition_from_payload(_value(replay, "payload_json", 0))
            )
            return (recovered or transition), False

        cursor = self._conn.execute(
            """
            UPDATE clientplatform_sales_conversation_state
            SET state=?, version=version+1, updated_at=?
            WHERE lead_id=? AND business_id=? AND version=?
            """,
            (
                transition.current.value,
                timestamp,
                lead.id,
                lead.business_id,
                version,
            ),
        )
        if int(getattr(cursor, "rowcount", 1) or 0) != 1:
            raise RuntimeError("sales_conversation_concurrent_update")

        latest_lead = self._sales.get_lead(actor=actor, lead_id=lead.id)
        target = coarse_sales_stage(
            transition.current,
            previous_stage=latest_lead.stage,
        )
        rank = {
            SalesLeadStage.NEW: 0,
            SalesLeadStage.CONTACTED: 1,
            SalesLeadStage.QUALIFIED: 2,
            SalesLeadStage.CHECKOUT: 3,
            SalesLeadStage.WON: 4,
        }
        should_write = (
            (target == SalesLeadStage.WON and latest_lead.stage != SalesLeadStage.WON)
            or (
                target == SalesLeadStage.LOST
                and latest_lead.stage != SalesLeadStage.WON
            )
            or (
                latest_lead.stage not in {SalesLeadStage.WON, SalesLeadStage.LOST}
                and target in rank
                and rank[target] > rank.get(latest_lead.stage, -1)
            )
        )
        if should_write:
            self._sales.set_stage(
                actor=actor,
                lead_id=lead.id,
                stage=target,
                now=now,
            )
        return transition, True

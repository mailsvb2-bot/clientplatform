from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from clientplatform.domain.sales_metrics import SalesFunnelCounts, SalesFunnelSnapshot
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


_REACHED_ENGAGED = frozenset(
    {
        "engaged",
        "need_known",
        "qualified",
        "offer_presented",
        "checkout",
        "won",
        "handoff",
    }
)
_REACHED_QUALIFIED = frozenset(
    {"qualified", "offer_presented", "checkout", "won"}
)
_REACHED_CHECKOUT = frozenset({"checkout", "won"})


def _rowdict(row: Any) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return {str(key): row[key] for key in row.keys()}
    return dict(row)


class SalesMetricsRepository:
    """Read-only, tenant-scoped evidence metrics; no synthetic AI activity counts."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _current(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id, business_id=actor.business_id
        )
        current.assert_can_view_promotion_analytics()
        return current

    @staticmethod
    def _counts_from_flags(items: list[dict[str, object]], handoffs: int) -> SalesFunnelCounts:
        return SalesFunnelCounts(
            discovered=len(items),
            engaged=sum(bool(item["engaged"]) for item in items),
            qualified=sum(bool(item["qualified"]) for item in items),
            checkout=sum(bool(item["checkout"]) for item in items),
            won=sum(bool(item["won"]) for item in items),
            lost=sum(bool(item["lost"]) for item in items),
            open_handoffs=max(0, int(handoffs)),
        )

    def snapshot(self, *, actor: TenantContext) -> SalesFunnelSnapshot:
        current = self._current(actor)
        lead_rows = [
            _rowdict(row)
            for row in self._conn.execute(
                """
                SELECT id, source_kind, stage
                FROM clientplatform_sales_leads
                WHERE business_id=?
                """,
                (current.business_id,),
            ).fetchall()
        ]
        lead_ids = {str(row["id"]) for row in lead_rows}
        reached: dict[str, set[str]] = defaultdict(set)
        if lead_ids:
            for row in self._conn.execute(
                """
                SELECT lead_id, payload_json
                FROM clientplatform_sales_events
                WHERE business_id=? AND event_type='conversation_transition'
                """,
                (current.business_id,),
            ).fetchall():
                item = _rowdict(row)
                lead_id = str(item.get("lead_id") or "")
                if lead_id not in lead_ids:
                    continue
                try:
                    payload = json.loads(str(item.get("payload_json") or "{}"))
                except (TypeError, ValueError):
                    payload = {}
                state = str((payload or {}).get("to") or "")
                if state:
                    reached[lead_id].add(state)

        flags: list[dict[str, object]] = []
        by_source_flags: dict[str, list[dict[str, object]]] = defaultdict(list)
        for lead in lead_rows:
            lead_id = str(lead["id"])
            stage = str(lead.get("stage") or "new")
            states = set(reached.get(lead_id, set()))
            if stage in {"contacted", "qualified", "checkout", "won"}:
                states.add("engaged")
            if stage in {"qualified", "checkout", "won"}:
                states.add("qualified")
            if stage in {"checkout", "won"}:
                states.add("checkout")
            if stage == "won":
                states.add("won")
            won = "won" in states or stage == "won"
            item = {
                "engaged": bool(states & _REACHED_ENGAGED),
                "qualified": bool(states & _REACHED_QUALIFIED),
                "checkout": bool(states & _REACHED_CHECKOUT),
                "won": won,
                "lost": (stage == "lost" or "lost" in states) and not won,
            }
            flags.append(item)
            source = str(lead.get("source_kind") or "unknown")
            by_source_flags[source].append(item)

        handoff_rows = self._conn.execute(
            """
            SELECT lead_id
            FROM clientplatform_sales_handoffs
            WHERE business_id=? AND status IN ('open','claimed')
            """,
            (current.business_id,),
        ).fetchall()
        handoff_ids = [str(_rowdict(row).get("lead_id") or "") for row in handoff_rows]
        handoffs_by_source: dict[str, int] = defaultdict(int)
        source_by_lead = {
            str(item["id"]): str(item.get("source_kind") or "unknown")
            for item in lead_rows
        }
        for lead_id in handoff_ids:
            handoffs_by_source[source_by_lead.get(lead_id, "unknown")] += 1

        return SalesFunnelSnapshot(
            total=self._counts_from_flags(flags, len(handoff_ids)),
            by_source={
                source: self._counts_from_flags(
                    source_items, handoffs_by_source.get(source, 0)
                )
                for source, source_items in sorted(by_source_flags.items())
            },
        )

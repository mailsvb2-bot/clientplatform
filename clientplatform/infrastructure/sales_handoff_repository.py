from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.sales_handoff import HandoffSignal
from clientplatform.domain.tenancy import PlatformRole, TenantContext, normalize_uuid
from clientplatform.infrastructure.sales_repository import SalesRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository



_SEVERITY_RANK = {"normal": 0, "high": 1, "urgent": 2}
_MAX_CONTEXT_BYTES = 32 * 1024


def _context_json(context: dict[str, object] | None) -> str:
    try:
        payload = json.dumps(
            dict(context or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("sales handoff context must be JSON serializable") from exc
    if len(payload.encode("utf-8")) > _MAX_CONTEXT_BYTES:
        raise ValueError("sales handoff context is too large")
    return payload

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rowdict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, "keys"):
        return {str(key): row[key] for key in row.keys()}
    return dict(row)


class SalesHandoffRepository:
    """Tenant-scoped human handoff queue with preserved conversation context."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)
        self._sales = SalesRepository(conn)

    def _current(self, actor: TenantContext, *, manage: bool) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id, business_id=actor.business_id
        )
        if manage:
            current.assert_can_manage_customer_records()
        else:
            current.assert_can_view_customer_records()
        return current

    def open(
        self,
        *,
        actor: TenantContext,
        lead_id: str,
        signal: HandoffSignal,
        context: dict[str, object] | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        current = self._current(actor, manage=True)
        lead = self._sales.get_lead(actor=current, lead_id=lead_id)
        timestamp = str(now or _utc_now())
        handoff_id = str(uuid4())
        context_json = _context_json(context)
        replacement_context_json = None if context is None else context_json
        self._conn.execute(
            """
            INSERT INTO clientplatform_sales_handoffs(
                id, business_id, lead_id, reason, severity, summary,
                context_json, status, created_by_member_id, claimed_by_member_id,
                created_at, updated_at, resolved_at
            ) VALUES(?,?,?,?,?,?,?,'open',?,NULL,?,?,NULL)
            ON CONFLICT DO NOTHING
            """,
            (
                handoff_id,
                current.business_id,
                lead.id,
                signal.reason.value,
                signal.severity.value,
                signal.summary,
                context_json,
                current.membership_id,
                timestamp,
                timestamp,
            ),
        )
        row = self._conn.execute(
            """
            SELECT id
            FROM clientplatform_sales_handoffs
            WHERE business_id=? AND lead_id=? AND status IN ('open','claimed')
            ORDER BY created_at, id
            LIMIT 1
            """,
            (current.business_id, lead.id),
        ).fetchone()
        if row is None:
            raise RuntimeError("sales handoff open failed")
        active_id = str(row["id"] if hasattr(row, "keys") else row[0])
        active = self.get(actor=current, handoff_id=active_id)
        if active_id != handoff_id:
            is_escalation = (
                _SEVERITY_RANK[signal.severity.value]
                > _SEVERITY_RANK.get(str(active.get("severity") or "normal"), 0)
            )
            if is_escalation:
                self._conn.execute(
                    """
                    UPDATE clientplatform_sales_handoffs
                    SET reason=?, severity=?, summary=?,
                        context_json=COALESCE(?, context_json), updated_at=?
                    WHERE id=? AND business_id=? AND status IN ('open','claimed')
                    """,
                    (
                        signal.reason.value,
                        signal.severity.value,
                        signal.summary,
                        replacement_context_json,
                        timestamp,
                        active_id,
                        current.business_id,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE clientplatform_sales_handoffs
                    SET context_json=COALESCE(?, context_json), updated_at=?
                    WHERE id=? AND business_id=? AND status IN ('open','claimed')
                    """,
                    (
                        replacement_context_json,
                        timestamp,
                        active_id,
                        current.business_id,
                    ),
                )
            active = self.get(actor=current, handoff_id=active_id)
        return active

    def get(self, *, actor: TenantContext, handoff_id: str) -> dict[str, Any]:
        current = self._current(actor, manage=False)
        normalized = normalize_uuid(handoff_id, field_name="handoff_id")
        row = self._conn.execute(
            """
            SELECT * FROM clientplatform_sales_handoffs
            WHERE id=? AND business_id=? LIMIT 1
            """,
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise ValueError("sales handoff was not found in the active business")
        item = _rowdict(row)
        try:
            item["context"] = json.loads(str(item.pop("context_json") or "{}"))
        except (TypeError, ValueError):
            item["context"] = {}
        return item

    def list_open(self, *, actor: TenantContext) -> list[dict[str, Any]]:
        current = self._current(actor, manage=False)
        rows = self._conn.execute(
            """
            SELECT * FROM clientplatform_sales_handoffs
            WHERE business_id=? AND status IN ('open','claimed')
            ORDER BY
                CASE severity WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                created_at,
                id
            """,
            (current.business_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = _rowdict(row)
            try:
                item["context"] = json.loads(str(item.pop("context_json") or "{}"))
            except (TypeError, ValueError):
                item["context"] = {}
            result.append(item)
        return result

    def claim(
        self,
        *,
        actor: TenantContext,
        handoff_id: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        current = self._current(actor, manage=True)
        item = self.get(actor=current, handoff_id=handoff_id)
        if str(item["status"]) == "resolved":
            raise ValueError("resolved sales handoff cannot be claimed")
        claimed_by = item.get("claimed_by_member_id")
        if str(item["status"]) == "claimed":
            if claimed_by == current.membership_id:
                self._sales.assign_member(
                    actor=current,
                    lead_id=str(item["lead_id"]),
                    member_id=current.membership_id,
                    now=now,
                )
                return item
            raise PermissionError("sales handoff is already claimed by another member")
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_sales_handoffs
            SET status='claimed', claimed_by_member_id=?, updated_at=?
            WHERE id=? AND business_id=? AND status='open'
            """,
            (
                current.membership_id,
                timestamp,
                item["id"],
                current.business_id,
            ),
        )
        if int(getattr(cursor, "rowcount", 1) or 0) != 1:
            latest = self.get(actor=current, handoff_id=str(item["id"]))
            if latest.get("claimed_by_member_id") == current.membership_id:
                self._sales.assign_member(
                    actor=current,
                    lead_id=str(latest["lead_id"]),
                    member_id=current.membership_id,
                    now=now,
                )
                return latest
            raise PermissionError("sales handoff was claimed concurrently")
        self._sales.assign_member(
            actor=current,
            lead_id=str(item["lead_id"]),
            member_id=current.membership_id,
            now=now,
        )
        return self.get(actor=current, handoff_id=item["id"])

    def resolve(
        self,
        *,
        actor: TenantContext,
        handoff_id: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        current = self._current(actor, manage=True)
        item = self.get(actor=current, handoff_id=handoff_id)
        claimed_by = item.get("claimed_by_member_id")
        if (
            claimed_by
            and claimed_by != current.membership_id
            and current.role not in {PlatformRole.OWNER, PlatformRole.ADMINISTRATOR}
        ):
            raise PermissionError("sales handoff is owned by another member")
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            UPDATE clientplatform_sales_handoffs
            SET status='resolved', resolved_at=?, updated_at=?
            WHERE id=? AND business_id=? AND status<>'resolved'
            """,
            (timestamp, timestamp, item["id"], current.business_id),
        )
        return self.get(actor=current, handoff_id=item["id"])

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.domain.visual_scene_plan import (
    VisualScenePlanReceipt,
    VisualScenePlanStatus,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


_PLAN_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _receipt(row: Any) -> VisualScenePlanReceipt:
    return VisualScenePlanReceipt(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        created_by_member_id=str(_value(row, "created_by_member_id", 2)),
        plan_key=str(_value(row, "plan_key", 3)),
        request_text=str(_value(row, "request_text", 4)),
        style_json=str(_value(row, "style_json", 5)),
        result_json=str(_value(row, "result_json", 6)),
        status=VisualScenePlanStatus(str(_value(row, "status", 7))),
        created_at=str(_value(row, "created_at", 8)),
        updated_at=str(_value(row, "updated_at", 9)),
    )


_SELECT = """
    SELECT id, business_id, created_by_member_id, plan_key, request_text,
           style_json, result_json, status, created_at, updated_at
    FROM visual_scene_plan_receipts
"""


class VisualScenePlanReceiptRepository:
    """Durable idempotency boundary around a potentially paid text-AI plan call."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _actor(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        return current

    def get_by_key(
        self,
        *,
        actor: TenantContext,
        plan_key: str,
    ) -> VisualScenePlanReceipt | None:
        current = self._actor(actor)
        key = str(plan_key or "").strip().lower()
        if not _PLAN_KEY_RE.fullmatch(key):
            raise ValueError("visual scene plan key is invalid")
        row = self._conn.execute(
            _SELECT
            + " WHERE business_id=? AND created_by_member_id=? AND plan_key=? LIMIT 1",
            (current.business_id, current.membership_id, key),
        ).fetchone()
        return None if row is None else _receipt(row)

    def claim(
        self,
        *,
        actor: TenantContext,
        plan_key: str,
        request_text: str,
        style_json: str,
        now: str | None = None,
    ) -> tuple[VisualScenePlanReceipt, bool]:
        current = self._actor(actor)
        key = str(plan_key or "").strip().lower()
        request = " ".join(
            str(request_text or "").replace("\x00", " ").split()
        ).strip()
        style = str(style_json or "").strip()
        if not _PLAN_KEY_RE.fullmatch(key):
            raise ValueError("visual scene plan key is invalid")
        if not request or len(request) > 1500:
            raise ValueError("visual scene plan request is invalid")
        if not style or len(style) > 2500 or "\x00" in style:
            raise ValueError("visual scene plan style is invalid")

        existing = self.get_by_key(actor=current, plan_key=key)
        if existing is not None:
            if existing.request_text != request or existing.style_json != style:
                raise ValueError("visual scene plan key collision")
            return existing, False

        timestamp = str(now or _iso_now())
        receipt_id = str(uuid.uuid4())
        cursor = self._conn.execute(
            """
            INSERT INTO visual_scene_plan_receipts(
                id,business_id,created_by_member_id,plan_key,request_text,
                style_json,result_json,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'','planning',?,?)
            ON CONFLICT(business_id,created_by_member_id,plan_key) DO NOTHING
            """,
            (
                receipt_id,
                current.business_id,
                current.membership_id,
                key,
                request,
                style,
                timestamp,
                timestamp,
            ),
        )
        created = int(getattr(cursor, "rowcount", 0) or 0) == 1
        receipt = self.get_by_key(actor=current, plan_key=key)
        if receipt is None:
            raise RuntimeError("visual scene plan claim was not persisted")
        if receipt.request_text != request or receipt.style_json != style:
            raise ValueError("visual scene plan key collision")
        return receipt, created

    def complete(
        self,
        *,
        actor: TenantContext,
        receipt_id: str,
        result_json: str,
        now: str | None = None,
    ) -> VisualScenePlanReceipt:
        current = self._actor(actor)
        normalized = normalize_uuid(receipt_id, field_name="receipt_id")
        result = str(result_json or "").strip()
        if not result or len(result) > 12000 or "\x00" in result:
            raise ValueError("visual scene plan result is invalid")
        timestamp = str(now or _iso_now())
        cursor = self._conn.execute(
            """
            UPDATE visual_scene_plan_receipts
            SET result_json=?, status='ready', updated_at=?
            WHERE id=? AND business_id=? AND created_by_member_id=?
              AND status='planning'
            """,
            (
                result,
                timestamp,
                normalized,
                current.business_id,
                current.membership_id,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ValueError("visual scene plan state changed")
        row = self._conn.execute(
            _SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise LookupError("visual scene plan receipt was not found")
        return _receipt(row)

    def mark_ambiguous(
        self,
        *,
        actor: TenantContext,
        receipt_id: str,
        now: str | None = None,
    ) -> VisualScenePlanReceipt:
        current = self._actor(actor)
        normalized = normalize_uuid(receipt_id, field_name="receipt_id")
        timestamp = str(now or _iso_now())
        self._conn.execute(
            """
            UPDATE visual_scene_plan_receipts
            SET status='ambiguous', updated_at=?
            WHERE id=? AND business_id=? AND created_by_member_id=?
              AND status='planning'
            """,
            (
                timestamp,
                normalized,
                current.business_id,
                current.membership_id,
            ),
        )
        row = self._conn.execute(
            _SELECT + " WHERE id=? AND business_id=? LIMIT 1",
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise LookupError("visual scene plan receipt was not found")
        return _receipt(row)


__all__ = ["VisualScenePlanReceiptRepository"]

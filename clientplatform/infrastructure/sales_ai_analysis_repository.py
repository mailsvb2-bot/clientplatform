from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from clientplatform.domain.tenancy import normalize_uuid


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _json_object(payload: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class SalesAIAnalysisRepository:
    """Deterministic latest-analysis projection ordered by provider source id."""

    def __init__(self, conn: Any):
        self._conn = conn

    def upsert_latest(
        self,
        *,
        business_id: str,
        lead_id: str,
        source_order_key: str,
        source_event_dedupe_key: str,
        analysis: Mapping[str, Any],
        provider: str,
        model: str,
        plan_id: str | None,
        action_kind: str | None,
        verified_offer: Mapping[str, Any] | None,
        now: datetime | None = None,
    ) -> bool:
        business = normalize_uuid(business_id, field_name="business_id")
        lead = normalize_uuid(lead_id, field_name="sales_lead_id")
        timestamp = _stamp(now or _utc_now())
        cursor = self._conn.execute(
            """
            INSERT INTO clientplatform_sales_ai_analysis_projection(
                business_id, lead_id, source_order_key, source_event_dedupe_key,
                analysis_json, provider, model, plan_id, action_kind,
                verified_offer_json, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(business_id, lead_id) DO UPDATE SET
                source_order_key=excluded.source_order_key,
                source_event_dedupe_key=excluded.source_event_dedupe_key,
                analysis_json=excluded.analysis_json,
                provider=excluded.provider,
                model=excluded.model,
                plan_id=excluded.plan_id,
                action_kind=excluded.action_kind,
                verified_offer_json=excluded.verified_offer_json,
                updated_at=excluded.updated_at
            WHERE excluded.source_order_key > clientplatform_sales_ai_analysis_projection.source_order_key
            """,
            (
                business,
                lead,
                source_order_key,
                str(source_event_dedupe_key),
                _json_object(analysis),
                str(provider),
                str(model),
                plan_id,
                action_kind,
                None if verified_offer is None else _json_object(verified_offer),
                timestamp,
            ),
        )
        return int(getattr(cursor, "rowcount", 1) or 0) == 1

    def get_latest(self, *, business_id: str, lead_id: str) -> dict[str, Any] | None:
        business = normalize_uuid(business_id, field_name="business_id")
        lead = normalize_uuid(lead_id, field_name="sales_lead_id")
        row = self._conn.execute(
            """
            SELECT source_order_key, source_event_dedupe_key, analysis_json,
                   provider, model, plan_id, action_kind, verified_offer_json, updated_at
            FROM clientplatform_sales_ai_analysis_projection
            WHERE business_id=? AND lead_id=? LIMIT 1
            """,
            (business, lead),
        ).fetchone()
        if row is None:
            return None
        try:
            analysis = json.loads(str(_value(row, "analysis_json", 2) or "{}"))
            offer_raw = _value(row, "verified_offer_json", 7)
            offer = None if offer_raw is None else json.loads(str(offer_raw))
        except json.JSONDecodeError as exc:
            raise ValueError("stored Sales AI projection JSON is invalid") from exc
        if not isinstance(analysis, dict) or (offer is not None and not isinstance(offer, dict)):
            raise ValueError("stored Sales AI projection shape is invalid")
        return {
            "source_order_key": str(_value(row, "source_order_key", 0)),
            "source_event_dedupe_key": str(_value(row, "source_event_dedupe_key", 1)),
            "analysis": analysis,
            "provider": str(_value(row, "provider", 3)),
            "model": str(_value(row, "model", 4)),
            "plan_id": _value(row, "plan_id", 5),
            "action_kind": _value(row, "action_kind", 6),
            "verified_offer": offer,
            "updated_at": str(_value(row, "updated_at", 8)),
        }

    def purge_expired(self, *, analysis_ttl_days: int, now: datetime | None = None) -> int:
        if isinstance(analysis_ttl_days, bool) or not isinstance(analysis_ttl_days, int) or not 1 <= analysis_ttl_days <= 365:
            raise ValueError("analysis_ttl_days must be 1..365")
        cutoff = _stamp((now or _utc_now()) - timedelta(days=analysis_ttl_days))
        cursor = self._conn.execute(
            "DELETE FROM clientplatform_sales_ai_analysis_projection WHERE updated_at<?",
            (cutoff,),
        )
        projection_count = max(int(getattr(cursor, "rowcount", 0) or 0), 0)
        redacted = _json_object({"redacted": True, "reason": "sales_ai_analysis_ttl"})
        event_cursor = self._conn.execute(
            """
            UPDATE clientplatform_sales_events
            SET payload_json=?
            WHERE event_type IN ('ai_sales_analysis','ai_sales_analysis_stale')
              AND occurred_at<? AND payload_json NOT LIKE '%\"redacted\":true%'
            """,
            (redacted, cutoff),
        )
        event_count = max(int(getattr(event_cursor, "rowcount", 0) or 0), 0)
        return projection_count + event_count


__all__ = ["SalesAIAnalysisRepository"]

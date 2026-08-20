from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.sales import SalesInvariantViolation, SalesLeadStage, can_contact
from clientplatform.domain.sales_followup import (
    MAX_SENT_FOLLOWUPS_PER_LEAD,
    SUPPORTED_FOLLOWUP_PLATFORMS,
    SalesFollowup,
    SalesFollowupStatus,
    SalesFollowupStopReason,
    is_quiet_time,
    is_stale_lead,
    next_allowed_followup_time,
    normalize_followup_message,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.sales_repository import SalesRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


@dataclass(frozen=True, slots=True)
class FollowupSendDecision:
    allowed: bool
    stop_reason: SalesFollowupStopReason | None = None
    defer_until: str | None = None


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _timestamp(value: datetime | str | None) -> datetime:
    if value is None:
        return _utc_now()
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _followup_from_row(row: Any) -> SalesFollowup:
    return SalesFollowup(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        lead_id=str(_value(row, "lead_id", 2)),
        customer_id=str(_value(row, "customer_id", 3)),
        platform=str(_value(row, "platform", 4)),
        customer_identity_id=str(_value(row, "customer_identity_id", 5)),
        connection_id=str(_value(row, "connection_id", 6)),
        message_text=str(_value(row, "message_text", 7)),
        scheduled_at=str(_value(row, "scheduled_at", 8)),
        status=SalesFollowupStatus(str(_value(row, "status", 9))),
        idempotency_key=str(_value(row, "idempotency_key", 10)),
        created_by_member_id=str(_value(row, "created_by_member_id", 11)),
        provider_dispatch_id=(None if _value(row, "provider_dispatch_id", 12) is None else str(_value(row, "provider_dispatch_id", 12))),
        queued_at=None if _value(row, "queued_at", 13) is None else str(_value(row, "queued_at", 13)),
        sent_at=None if _value(row, "sent_at", 14) is None else str(_value(row, "sent_at", 14)),
        stopped_at=None if _value(row, "stopped_at", 15) is None else str(_value(row, "stopped_at", 15)),
        stop_reason=None if _value(row, "stop_reason", 16) is None else str(_value(row, "stop_reason", 16)),
        created_at=str(_value(row, "created_at", 17)),
        updated_at=str(_value(row, "updated_at", 18)),
    )


_FOLLOWUP_SELECT = """
id,business_id,lead_id,customer_id,platform,customer_identity_id,connection_id,
message_text,scheduled_at,status,idempotency_key,created_by_member_id,
provider_dispatch_id,queued_at,sent_at,stopped_at,stop_reason,created_at,updated_at
""".strip()


class SalesFollowupRepository:
    """Durable, tenant-scoped U-009 follow-up state and send eligibility."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)
        self._sales = SalesRepository(conn)

    def _current(self, actor: TenantContext, *, manage: bool) -> TenantContext:
        current = self._tenancy.resolve_context(user_id=actor.user_id, business_id=actor.business_id)
        if manage:
            current.assert_can_manage_customer_records()
        else:
            current.assert_can_view_customer_records()
        return current

    def _optional_table_available(self, table: str) -> bool:
        if table not in {"clientplatform_sales_followups", "provider_dispatch_outbox"}:
            raise ValueError("unsupported optional follow-up table")
        try:
            self._conn.execute(f"SELECT 1 FROM {table} WHERE 1=0").fetchone()  # nosec B608 - fixed allowlist
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return False
            raise
        return True

    def get(self, *, actor: TenantContext, followup_id: str) -> SalesFollowup:
        current = self._current(actor, manage=False)
        normalized = normalize_uuid(followup_id, field_name="followup_id")
        row = self._conn.execute(
            f"SELECT {_FOLLOWUP_SELECT} FROM clientplatform_sales_followups WHERE id=? AND business_id=? LIMIT 1",  # nosec B608
            (normalized, current.business_id),
        ).fetchone()
        if row is None:
            raise ValueError("sales follow-up was not found in the active business")
        return _followup_from_row(row)

    def active_for_lead(self, *, actor: TenantContext, lead_id: str) -> SalesFollowup | None:
        current = self._current(actor, manage=False)
        lead = normalize_uuid(lead_id, field_name="lead_id")
        row = self._conn.execute(
            f"SELECT {_FOLLOWUP_SELECT} FROM clientplatform_sales_followups "  # nosec B608
            "WHERE business_id=? AND lead_id=? AND status IN ('scheduled','queued') "
            "ORDER BY created_at DESC,id DESC LIMIT 1",
            (current.business_id, lead),
        ).fetchone()
        return None if row is None else _followup_from_row(row)

    def _business_timezone(self, business_id: str) -> str:
        row = self._conn.execute(
            "SELECT timezone FROM business_profiles WHERE business_id=? AND status IN ('draft','active') LIMIT 1",
            (business_id,),
        ).fetchone()
        if row is None:
            raise SalesInvariantViolation("business timezone is required before scheduling follow-up")
        return str(_value(row, "timezone", 0))

    def _target(self, *, business_id: str, customer_id: str, platform: str, source_ref: str | None) -> tuple[str, str]:
        identity = self._conn.execute(
            """
            SELECT id
            FROM customer_identities
            WHERE business_id=? AND customer_id=? AND platform=? AND status='active'
            ORDER BY created_at,id LIMIT 1
            """,
            (business_id, customer_id, platform),
        ).fetchone()
        if identity is None:
            raise SalesInvariantViolation("follow-up requires an active identity on the original channel")
        identity_id = str(_value(identity, "id", 0))

        connection = None
        source = str(source_ref or "").strip()
        if platform == "telegram" and source.startswith("managed_bot:"):
            managed_bot_id = source.split(":", 1)[1]
            try:
                normalized_bot = normalize_uuid(managed_bot_id, field_name="managed_bot_id")
            except ValueError:
                normalized_bot = ""
            if normalized_bot:
                connection = self._conn.execute(
                    """
                    SELECT c.id
                    FROM managed_bots m
                    JOIN connections c
                      ON c.id=m.connection_id AND c.business_id=m.business_id
                     AND c.platform=m.platform
                    WHERE m.id=? AND m.business_id=? AND m.status='active'
                      AND c.status='active'
                    LIMIT 1
                    """,
                    (normalized_bot, business_id),
                ).fetchone()
        if connection is None:
            connection_types = {
                "telegram": ("telegram_shared_bot", "telegram_managed_bot"),
                "vk": ("vk_community",),
                "max": ("max_shared_bot", "max_personal_bot"),
            }[platform]
            placeholders = ",".join("?" for _ in connection_types)
            connection = self._conn.execute(
                "SELECT id FROM connections WHERE business_id=? AND platform=? AND status='active' "
                f"AND connection_type IN ({placeholders}) ORDER BY created_at,id LIMIT 1",  # nosec B608
                (business_id, platform, *connection_types),
            ).fetchone()
        if connection is None:
            raise SalesInvariantViolation("follow-up requires an active connection on the original channel")
        return identity_id, str(_value(connection, "id", 0))

    def _suppressed(self, *, business_id: str, customer_id: str, platform: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM clientplatform_sales_contact_suppressions WHERE business_id=? AND customer_id=? AND platform=? LIMIT 1",
            (business_id, customer_id, platform),
        ).fetchone()
        return row is not None

    def _has_conversion_since(self, *, business_id: str, customer_id: str, since: str) -> SalesFollowupStopReason | None:
        row = self._conn.execute(
            """
            SELECT outcome_type
            FROM business_outcome_events
            WHERE business_id=? AND customer_id=? AND occurred_at>=?
              AND outcome_type IN (
                'booking_created','booking_confirmed','booking_completed','order_paid'
              )
            ORDER BY occurred_at DESC,id DESC LIMIT 1
            """,
            (business_id, customer_id, since),
        ).fetchone()
        if row is None:
            return None
        outcome = str(_value(row, "outcome_type", 0))
        return SalesFollowupStopReason.PAYMENT if outcome == "order_paid" else SalesFollowupStopReason.BOOKING

    def schedule(
        self,
        *,
        actor: TenantContext,
        lead_id: str,
        message_text: str,
        scheduled_at: datetime | str,
        request_key: str,
        now: datetime | str | None = None,
    ) -> SalesFollowup:
        current = self._current(actor, manage=True)
        lead = self._sales.get_lead(actor=current, lead_id=lead_id)
        if lead.stage in {SalesLeadStage.WON, SalesLeadStage.LOST}:
            raise SalesInvariantViolation("closed sales lead cannot receive follow-up")
        if not can_contact(lead.contact_basis):
            raise SalesInvariantViolation("follow-up is forbidden without an active contact basis")
        platform = str(lead.source_kind or "").strip().lower()
        if platform not in SUPPORTED_FOLLOWUP_PLATFORMS:
            raise SalesInvariantViolation("follow-up can only use the customer's original Telegram, VK or MAX channel")
        if self._suppressed(business_id=current.business_id, customer_id=lead.customer_id, platform=platform):
            raise SalesInvariantViolation("customer opted out of follow-up on this channel")
        if self._has_conversion_since(
            business_id=current.business_id,
            customer_id=lead.customer_id,
            since=lead.created_at,
        ) is not None:
            raise SalesInvariantViolation("customer already booked or paid after this lead was created")
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM clientplatform_sales_followups WHERE business_id=? AND lead_id=? AND status='sent'",
            (current.business_id, lead.id),
        ).fetchone()
        if int(_value(row, "c", 0) or 0) >= MAX_SENT_FOLLOWUPS_PER_LEAD:
            raise SalesInvariantViolation("follow-up frequency cap reached for this lead")

        requested = str(request_key or "").strip()
        if not requested or len(requested) > 500:
            raise ValueError("request_key must be 1..500 characters")
        message = normalize_followup_message(message_text)
        current_time = _timestamp(now)
        requested_time = _timestamp(scheduled_at)
        if requested_time < current_time:
            raise ValueError("scheduled_at must not be in the past")
        timezone_name = self._business_timezone(current.business_id)
        allowed_time = next_allowed_followup_time(requested_time, timezone_name=timezone_name)
        identity_id, connection_id = self._target(
            business_id=current.business_id,
            customer_id=lead.customer_id,
            platform=platform,
            source_ref=lead.source_ref,
        )
        digest = hashlib.sha256(requested.encode("utf-8")).hexdigest()
        idempotency_key = f"u009:{digest}"
        existing = self._conn.execute(
            f"SELECT {_FOLLOWUP_SELECT} FROM clientplatform_sales_followups WHERE business_id=? AND idempotency_key=? LIMIT 1",  # nosec B608
            (current.business_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            item = _followup_from_row(existing)
            if item.lead_id != lead.id or item.message_text != message:
                raise SalesInvariantViolation("follow-up idempotency key belongs to different work")
            return item
        active = self._conn.execute(
            "SELECT id FROM clientplatform_sales_followups WHERE business_id=? AND lead_id=? AND status IN ('scheduled','queued') LIMIT 1",
            (current.business_id, lead.id),
        ).fetchone()
        if active is not None:
            raise SalesInvariantViolation("lead already has an active follow-up")

        followup_id = str(uuid4())
        stamp = current_time.isoformat()
        self._conn.execute(
            """
            INSERT INTO clientplatform_sales_followups(
                id,business_id,lead_id,customer_id,platform,customer_identity_id,
                connection_id,message_text,scheduled_at,status,idempotency_key,
                created_by_member_id,provider_dispatch_id,queued_at,sent_at,
                stopped_at,stop_reason,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,'scheduled',?,?,NULL,NULL,NULL,NULL,NULL,?,?)
            """,
            (
                followup_id,
                current.business_id,
                lead.id,
                lead.customer_id,
                platform,
                identity_id,
                connection_id,
                message,
                allowed_time.isoformat(),
                idempotency_key,
                current.membership_id,
                stamp,
                stamp,
            ),
        )
        self._sales.record_event(
            actor=current,
            lead_id=lead.id,
            event_type="followup_scheduled",
            dedupe_key=f"followup-scheduled:{followup_id}",
            payload={
                "followup_id": followup_id,
                "platform": platform,
                "scheduled_at": allowed_time.isoformat(),
                "approval": "owner_explicit",
            },
            now=stamp,
        )
        return self.get(actor=current, followup_id=followup_id)

    def suppress_channel(
        self,
        *,
        actor: TenantContext,
        lead_id: str,
        reason: str = "opt_out",
        now: datetime | str | None = None,
    ) -> int:
        current = self._current(actor, manage=True)
        lead = self._sales.get_lead(actor=current, lead_id=lead_id)
        platform = str(lead.source_kind or "").strip().lower()
        if platform not in SUPPORTED_FOLLOWUP_PLATFORMS:
            raise SalesInvariantViolation("lead has no supported messenger channel")
        selected_reason = str(reason or "opt_out").strip().lower()
        if selected_reason not in {"opt_out", "do_not_contact"}:
            raise ValueError("unsupported contact suppression reason")
        stamp = _timestamp(now).isoformat()
        self._conn.execute(
            """
            INSERT INTO clientplatform_sales_contact_suppressions(
                business_id,customer_id,platform,reason,updated_by_member_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(business_id,customer_id,platform) DO UPDATE SET
                reason=excluded.reason,updated_by_member_id=excluded.updated_by_member_id,
                updated_at=excluded.updated_at
            """,
            (
                current.business_id,
                lead.customer_id,
                platform,
                selected_reason,
                current.membership_id,
                stamp,
                stamp,
            ),
        )
        stopped = self._stop_active(
            business_id=current.business_id,
            lead_id=lead.id,
            reason=SalesFollowupStopReason.OPT_OUT,
            now=stamp,
        )
        self._sales.record_event(
            actor=current,
            lead_id=lead.id,
            event_type="followup_opt_out",
            dedupe_key=f"followup-opt-out:{lead.customer_id}:{platform}",
            payload={"platform": platform, "reason": selected_reason},
            now=stamp,
        )
        return stopped

    def cancel_active(
        self,
        *,
        actor: TenantContext,
        lead_id: str,
        now: datetime | str | None = None,
    ) -> int:
        current = self._current(actor, manage=True)
        lead = self._sales.get_lead(actor=current, lead_id=lead_id)
        return self._stop_active(
            business_id=current.business_id,
            lead_id=lead.id,
            reason=SalesFollowupStopReason.OWNER_CANCELLED,
            now=_timestamp(now).isoformat(),
            status="cancelled",
        )

    def stop_for_inbound(
        self,
        *,
        business_id: str,
        lead_id: str,
        now: datetime | str | None = None,
    ) -> int:
        return self._stop_active(
            business_id=normalize_uuid(business_id, field_name="business_id"),
            lead_id=normalize_uuid(lead_id, field_name="lead_id"),
            reason=SalesFollowupStopReason.REPLY,
            now=_timestamp(now).isoformat(),
        )

    def stop_for_customer_conversion(
        self,
        *,
        business_id: str,
        customer_id: str,
        reason: SalesFollowupStopReason,
        now: datetime | str | None = None,
    ) -> int:
        if reason not in {SalesFollowupStopReason.BOOKING, SalesFollowupStopReason.PAYMENT}:
            raise ValueError("conversion stop reason must be booking or payment")
        business = normalize_uuid(business_id, field_name="business_id")
        customer = normalize_uuid(customer_id, field_name="customer_id")
        stamp = _timestamp(now).isoformat()
        if not self._optional_table_available("clientplatform_sales_followups"):
            return 0
        rows = self._conn.execute(
            "SELECT lead_id FROM clientplatform_sales_followups WHERE business_id=? AND customer_id=? AND status IN ('scheduled','queued')",
            (business, customer),
        ).fetchall()
        total = 0
        for row in rows:
            total += self._stop_active(
                business_id=business,
                lead_id=str(_value(row, "lead_id", 0)),
                reason=reason,
                now=stamp,
            )
        return total

    def _stop_active(
        self,
        *,
        business_id: str,
        lead_id: str,
        reason: SalesFollowupStopReason,
        now: str,
        status: str = "stopped",
    ) -> int:
        if not self._optional_table_available("clientplatform_sales_followups"):
            return 0
        rows = self._conn.execute(
            "SELECT id FROM clientplatform_sales_followups WHERE business_id=? AND lead_id=? AND status IN ('scheduled','queued')",
            (business_id, lead_id),
        ).fetchall()
        ids = [str(_value(row, "id", 0)) for row in rows]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        self._conn.execute(
            "UPDATE clientplatform_sales_followups SET status=?,stopped_at=?,stop_reason=?,updated_at=? "
            f"WHERE business_id=? AND id IN ({placeholders}) AND status IN ('scheduled','queued')",  # nosec B608
            (status, now, reason.value, now, business_id, *ids),
        )
        if self._optional_table_available("provider_dispatch_outbox"):
            self._conn.execute(
                "UPDATE provider_dispatch_outbox SET status='cancelled',updated_at=?,locked_at=NULL,lock_token=NULL,last_error=? "
                f"WHERE business_id=? AND source_kind='sales_followup' AND source_id IN ({placeholders}) "  # nosec B608
                "AND status IN ('pending','retry')",
                (now, f"sales_followup_{reason.value}", business_id, *ids),
            )
        for followup_id in ids:
            self._conn.execute(
                """
                INSERT INTO clientplatform_sales_events(
                    id,business_id,lead_id,event_type,dedupe_key,payload_json,occurred_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(business_id,lead_id,dedupe_key) DO NOTHING
                """,
                (
                    str(uuid4()),
                    business_id,
                    lead_id,
                    "followup_stopped",
                    f"followup-stop:{followup_id}:{reason.value}",
                    json.dumps(
                        {"followup_id": followup_id, "reason": reason.value},
                        sort_keys=True,
                    ),
                    now,
                ),
            )
        return len(ids)

    def stop_invalid_queued(
        self,
        *,
        now: datetime | str | None = None,
        limit: int = 100,
    ) -> int:
        current = _timestamp(now)
        rows = self._conn.execute(
            """
            SELECT id,business_id,lead_id
            FROM clientplatform_sales_followups
            WHERE status='queued'
            ORDER BY scheduled_at,id
            LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        stopped = 0
        for row in rows:
            followup_id = str(_value(row, "id", 0))
            business_id = str(_value(row, "business_id", 1))
            lead_id = str(_value(row, "lead_id", 2))
            decision = self.decision_for_send(
                business_id=business_id,
                followup_id=followup_id,
                now=current,
            )
            if decision.allowed or decision.stop_reason is None:
                continue
            stopped += self._stop_active(
                business_id=business_id,
                lead_id=lead_id,
                reason=decision.stop_reason,
                now=current.isoformat(),
            )
        return stopped

    def mark_stale_owner_reminders(
        self,
        *,
        now: datetime | str | None = None,
        limit: int = 100,
    ) -> int:
        current = _timestamp(now)
        selected_limit = max(1, min(int(limit), 500))
        rows = self._conn.execute(
            """
            SELECT l.id,l.business_id,l.last_signal_at
            FROM clientplatform_sales_leads l
            WHERE l.stage IN ('new','contacted','qualified','checkout')
              AND l.next_action IS NULL AND l.due_at IS NULL
            ORDER BY l.last_signal_at,l.id
            LIMIT ?
            """,
            (selected_limit,),
        ).fetchall()
        marked = 0
        stamp = current.isoformat()
        for row in rows:
            last_signal = str(_value(row, "last_signal_at", 2))
            if not is_stale_lead(last_signal, now=current):
                continue
            lead_id = str(_value(row, "id", 0))
            business_id = str(_value(row, "business_id", 1))
            dedupe = f"u009-stale:{last_signal}"
            cursor = self._conn.execute(
                """
                INSERT INTO clientplatform_sales_events(
                    id,business_id,lead_id,event_type,dedupe_key,payload_json,occurred_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(business_id,lead_id,dedupe_key) DO NOTHING
                """,
                (
                    str(uuid4()),
                    business_id,
                    lead_id,
                    "followup_owner_reminder",
                    dedupe,
                    json.dumps({"stale_since": last_signal}, sort_keys=True),
                    stamp,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                continue
            updated = self._conn.execute(
                """
                UPDATE clientplatform_sales_leads
                SET next_action='Связаться с клиентом: давно нет ответа',due_at=?,updated_at=?
                WHERE id=? AND business_id=? AND stage IN ('new','contacted','qualified','checkout')
                  AND next_action IS NULL AND due_at IS NULL
                """,
                (stamp, stamp, lead_id, business_id),
            )
            marked += max(0, int(getattr(updated, "rowcount", 0) or 0))
        return marked

    def decision_for_send(
        self,
        *,
        business_id: str,
        followup_id: str,
        now: datetime | str | None = None,
    ) -> FollowupSendDecision:
        business = normalize_uuid(business_id, field_name="business_id")
        followup = normalize_uuid(followup_id, field_name="followup_id")
        row = self._conn.execute(
            """
            SELECT f.status,f.customer_id,f.platform,f.customer_identity_id,f.connection_id,
                   f.created_at,l.stage,l.contact_basis,l.source_kind,l.last_signal_at,
                   ci.status AS identity_status,c.status AS connection_status,bp.timezone,f.stop_reason
            FROM clientplatform_sales_followups f
            JOIN clientplatform_sales_leads l
              ON l.id=f.lead_id AND l.business_id=f.business_id
            LEFT JOIN customer_identities ci
              ON ci.id=f.customer_identity_id AND ci.business_id=f.business_id
             AND ci.platform=f.platform
            LEFT JOIN connections c
              ON c.id=f.connection_id AND c.business_id=f.business_id
             AND c.platform=f.platform
            LEFT JOIN business_profiles bp ON bp.business_id=f.business_id
            WHERE f.id=? AND f.business_id=? LIMIT 1
            """,
            (followup, business),
        ).fetchone()
        if row is None:
            return FollowupSendDecision(False, SalesFollowupStopReason.OWNER_CANCELLED)
        if str(_value(row, "status", 0)) != "queued":
            raw_reason = str(_value(row, "stop_reason", 13) or "").strip()
            try:
                reason = SalesFollowupStopReason(raw_reason)
            except ValueError:
                reason = SalesFollowupStopReason.OWNER_CANCELLED
            return FollowupSendDecision(False, reason)
        customer_id = str(_value(row, "customer_id", 1))
        platform = str(_value(row, "platform", 2))
        created_at = str(_value(row, "created_at", 5))
        stage = str(_value(row, "stage", 6))
        basis = str(_value(row, "contact_basis", 7))
        source_kind = str(_value(row, "source_kind", 8))
        identity_status = str(_value(row, "identity_status", 10) or "")
        connection_status = str(_value(row, "connection_status", 11) or "")
        timezone_name = str(_value(row, "timezone", 12) or "")

        if stage in {"won", "lost"}:
            return FollowupSendDecision(False, SalesFollowupStopReason.LEAD_CLOSED)
        if basis == "none":
            return FollowupSendDecision(False, SalesFollowupStopReason.CONTACT_FORBIDDEN)
        if source_kind != platform:
            return FollowupSendDecision(False, SalesFollowupStopReason.CHANNEL_UNAVAILABLE)
        if identity_status != "active":
            return FollowupSendDecision(False, SalesFollowupStopReason.IDENTITY_REVOKED)
        if connection_status != "active":
            return FollowupSendDecision(False, SalesFollowupStopReason.CHANNEL_UNAVAILABLE)
        if self._suppressed(business_id=business, customer_id=customer_id, platform=platform):
            return FollowupSendDecision(False, SalesFollowupStopReason.OPT_OUT)
        conversion = self._has_conversion_since(
            business_id=business,
            customer_id=customer_id,
            since=created_at,
        )
        if conversion is not None:
            return FollowupSendDecision(False, conversion)
        count = self._conn.execute(
            "SELECT COUNT(*) AS c FROM clientplatform_sales_followups WHERE business_id=? AND lead_id=(SELECT lead_id FROM clientplatform_sales_followups WHERE id=? AND business_id=?) AND status='sent'",
            (business, followup, business),
        ).fetchone()
        if int(_value(count, "c", 0) or 0) >= MAX_SENT_FOLLOWUPS_PER_LEAD:
            return FollowupSendDecision(False, SalesFollowupStopReason.FREQUENCY_CAP)
        current = _timestamp(now)
        if not timezone_name:
            return FollowupSendDecision(False, SalesFollowupStopReason.CHANNEL_UNAVAILABLE)
        if is_quiet_time(current, timezone_name=timezone_name):
            return FollowupSendDecision(
                False,
                None,
                next_allowed_followup_time(current, timezone_name=timezone_name).isoformat(),
            )
        return FollowupSendDecision(True)

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from clientplatform.domain.outcomes import (
    BusinessOutcomeEvent,
    OutcomeMoney,
    OutcomeSource,
    OutcomeType,
)
from clientplatform.domain.sales import ContactBasis, SalesInvariantViolation, SalesLeadStage
from clientplatform.domain.sales_followup import SalesFollowupStopReason
from clientplatform.infrastructure import ConnectionRepository, DispatchOutboxRepository
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.outcome_repository import OutcomeRepository
from clientplatform.infrastructure.sales_followup_repository import SalesFollowupRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.privacy_manifest import TENANT_POLICIES, validate_clientplatform_privacy_manifest
from services.db.schema import create_or_update_tables


class ClientPlatformSalesU009Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        create_or_update_tables(self.conn)
        self.tenancy = TenancyRepository(self.conn)
        access = self.tenancy.create_business(owner_user_id=901, name="U009 Practice")
        self.owner = self.tenancy.resolve_context(user_id=901, business_id=access.business.id)
        ActivityRepository(self.conn).upsert_profile(
            actor=self.owner,
            activity_description="Сервисная компания",
            timezone_name="Europe/Moscow",
            now="2026-08-19T07:00:00+00:00",
        )
        self.customer = CustomerRepository(self.conn).create_customer(
            actor=self.owner,
            display_name="Анна",
        )
        self.identity = CustomerRepository(self.conn).attach_identity(
            actor=self.owner,
            customer_id=self.customer.id,
            platform="telegram",
            external_subject="7009001",
        )
        connections = ConnectionRepository(self.conn)
        connection = connections.create_connection(
            actor=self.owner,
            platform="telegram",
            connection_type="telegram_shared_bot",
            external_account_id="u009-bot",
            credential_reference="secret://clientplatform/u009/telegram",
            permissions=("send_messages",),
        )
        self.connection = connections.activate_connection(
            actor=self.owner,
            connection_id=connection.id,
        )
        self.sales = SalesRepository(self.conn)
        self.followups = SalesFollowupRepository(self.conn)
        self.outbox = DispatchOutboxRepository(self.conn)
        self.lead = self.sales.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="telegram:u009:anna",
            customer_id=self.customer.id,
            source_kind="telegram",
            source_ref="inbound_test",
            contact_basis=ContactBasis.INBOUND,
            now="2026-08-19T08:00:00+00:00",
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _queue(
        self,
        *,
        request_key: str = "req-1",
        scheduled_at: str = "2026-08-20T10:00:00+00:00",
        now: str = "2026-08-20T08:00:00+00:00",
    ):
        followup = self.followups.schedule(
            actor=self.owner,
            lead_id=self.lead.id,
            message_text="Анна, добрый день. Подсказать по вашему вопросу?",
            scheduled_at=scheduled_at,
            request_key=request_key,
            now=now,
        )
        dispatch = self.outbox.materialize_sales_followup(
            actor=self.owner,
            followup_id=followup.id,
            now=now,
        )
        return self.followups.get(actor=self.owner, followup_id=followup.id), dispatch

    def test_privacy_manifest_covers_followup_and_opt_out_state(self) -> None:
        report = validate_clientplatform_privacy_manifest(self.conn, strict=True)
        self.assertTrue(report.ok)
        self.assertEqual(TENANT_POLICIES["clientplatform_sales_followups"].disposition, "erase")
        self.assertEqual(
            TENANT_POLICIES["clientplatform_sales_contact_suppressions"].disposition,
            "anonymize",
        )

    def test_owner_approved_followup_uses_original_channel_and_canonical_outbox(self) -> None:
        followup, dispatch = self._queue()
        self.assertEqual(followup.status.value, "queued")
        self.assertEqual(followup.platform, "telegram")
        self.assertEqual(followup.customer_identity_id, self.identity.id)
        self.assertEqual(followup.connection_id, self.connection.id)
        self.assertEqual(dispatch.source_kind, "sales_followup")
        self.assertEqual(dispatch.source_id, followup.id)
        self.assertEqual(dispatch.payload_kind.value, "text")
        self.assertEqual(dispatch.external_subject, "7009001")
        row = self.conn.execute(
            "SELECT sales_followup_id FROM provider_dispatch_outbox WHERE id=?",
            (dispatch.id,),
        ).fetchone()
        self.assertEqual(row["sales_followup_id"], followup.id)

    def test_same_request_and_materialization_are_replay_safe(self) -> None:
        first, dispatch = self._queue()
        repeated = self.followups.schedule(
            actor=self.owner,
            lead_id=self.lead.id,
            message_text=first.message_text,
            scheduled_at=first.scheduled_at,
            request_key="req-1",
            now="2026-08-20T08:00:00+00:00",
        )
        second_dispatch = self.outbox.materialize_sales_followup(
            actor=self.owner,
            followup_id=first.id,
            now="2026-08-20T08:00:01+00:00",
        )
        self.assertEqual(repeated.id, first.id)
        self.assertEqual(second_dispatch.id, dispatch.id)
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM provider_dispatch_outbox WHERE source_kind='sales_followup'"
        ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_quiet_hours_shift_schedule_and_claim_revalidates_again(self) -> None:
        shifted, _ = self._queue(
            request_key="quiet-schedule",
            scheduled_at="2026-08-20T20:00:00+00:00",
            now="2026-08-20T08:00:00+00:00",
        )
        self.assertEqual(shifted.scheduled_at, "2026-08-21T06:00:00+00:00")

        self.followups.cancel_active(actor=self.owner, lead_id=self.lead.id)
        queued, _ = self._queue(
            request_key="quiet-race",
            scheduled_at="2026-08-20T17:59:00+00:00",
            now="2026-08-20T17:00:00+00:00",
        )
        claimed = self.outbox.claim_due(
            limit=5,
            now=datetime.fromisoformat("2026-08-20T18:01:00+00:00"),
        )
        target = next(item for item in claimed if item.dispatch.source_id == queued.id)
        self.assertFalse(
            self.outbox.sales_followup_claim_can_cross_provider_boundary(
                target,
                now="2026-08-20T18:01:00+00:00",
            )
        )
        dispatch_row = self.conn.execute(
            "SELECT status,available_at,last_error FROM provider_dispatch_outbox WHERE id=?",
            (target.dispatch.id,),
        ).fetchone()
        self.assertEqual(dispatch_row["status"], "retry")
        self.assertEqual(dispatch_row["available_at"], "2026-08-21T06:00:00+00:00")
        self.assertEqual(dispatch_row["last_error"], "sales_followup_quiet_hours")

    def test_opt_out_stops_active_work_and_blocks_future_schedule(self) -> None:
        followup, dispatch = self._queue()
        stopped = self.followups.suppress_channel(
            actor=self.owner,
            lead_id=self.lead.id,
            now="2026-08-20T08:05:00+00:00",
        )
        self.assertEqual(stopped, 1)
        self.assertEqual(self.followups.get(actor=self.owner, followup_id=followup.id).stop_reason, "opt_out")
        row = self.conn.execute(
            "SELECT status FROM provider_dispatch_outbox WHERE id=?",
            (dispatch.id,),
        ).fetchone()
        self.assertEqual(row["status"], "cancelled")
        with self.assertRaisesRegex(SalesInvariantViolation, "opted out"):
            self.followups.schedule(
                actor=self.owner,
                lead_id=self.lead.id,
                message_text="Ещё одно сообщение",
                scheduled_at="2026-08-21T10:00:00+00:00",
                request_key="after-optout",
                now="2026-08-20T09:00:00+00:00",
            )

    def test_reply_between_claim_and_provider_call_cancels_leased_work(self) -> None:
        followup, _ = self._queue(
            request_key="reply-race",
            scheduled_at="2026-08-20T10:00:00+00:00",
            now="2026-08-20T08:00:00+00:00",
        )
        claimed = self.outbox.claim_due(
            limit=5,
            now=datetime.fromisoformat("2026-08-20T10:00:00+00:00"),
        )
        target = next(item for item in claimed if item.dispatch.source_id == followup.id)
        self.followups.stop_for_inbound(
            business_id=self.owner.business_id,
            lead_id=self.lead.id,
            now="2026-08-20T10:00:01+00:00",
        )
        self.assertFalse(
            self.outbox.sales_followup_claim_can_cross_provider_boundary(
                target,
                now="2026-08-20T10:00:02+00:00",
            )
        )
        row = self.conn.execute(
            "SELECT status,last_error FROM provider_dispatch_outbox WHERE id=?",
            (target.dispatch.id,),
        ).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertIn("reply", row["last_error"])

    def test_booking_and_payment_outcomes_stop_before_send(self) -> None:
        followup, _ = self._queue(request_key="conversion")
        OutcomeRepository(self.conn).append(
            BusinessOutcomeEvent(
                id=str(uuid4()),
                business_id=self.owner.business_id,
                outcome_type=OutcomeType.ORDER_PAID,
                occurred_at=datetime.fromisoformat("2026-08-20T09:00:00+00:00"),
                source=OutcomeSource(source_type="test_order", source_id="order-1"),
                customer_id=self.customer.id,
                subject_ref="order:1",
                money=OutcomeMoney(amount_minor=10000, currency="RUB"),
                idempotency_key="u009-order-paid-1",
                metadata={},
                metadata_version=1,
                created_at=datetime.fromisoformat("2026-08-20T09:00:00+00:00"),
            )
        )
        decision = self.followups.decision_for_send(
            business_id=self.owner.business_id,
            followup_id=followup.id,
            now="2026-08-20T10:00:00+00:00",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.stop_reason, SalesFollowupStopReason.PAYMENT)
        self.assertEqual(
            self.followups.stop_invalid_queued(now="2026-08-20T09:01:00+00:00"),
            1,
        )
        self.assertEqual(
            self.followups.get(actor=self.owner, followup_id=followup.id).stop_reason,
            "payment",
        )

    def test_closed_lead_stops_followup_and_outbox(self) -> None:
        followup, dispatch = self._queue(request_key="close")
        self.sales.set_stage(
            actor=self.owner,
            lead_id=self.lead.id,
            stage=SalesLeadStage.LOST,
            reason="не актуально",
            now="2026-08-20T08:05:00+00:00",
        )
        self.assertEqual(self.followups.get(actor=self.owner, followup_id=followup.id).stop_reason, "lead_closed")
        self.assertEqual(
            self.conn.execute("SELECT status FROM provider_dispatch_outbox WHERE id=?", (dispatch.id,)).fetchone()["status"],
            "cancelled",
        )

    def test_stale_lead_creates_one_owner_reminder_without_external_work(self) -> None:
        self.assertEqual(
            self.followups.mark_stale_owner_reminders(
                now="2026-08-20T09:00:01+00:00",
                limit=20,
            ),
            1,
        )
        self.assertEqual(
            self.followups.mark_stale_owner_reminders(
                now="2026-08-20T09:05:00+00:00",
                limit=20,
            ),
            0,
        )
        lead = self.sales.get_lead(actor=self.owner, lead_id=self.lead.id)
        self.assertEqual(lead.next_action, "Связаться с клиентом: давно нет ответа")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM provider_dispatch_outbox WHERE source_kind='sales_followup'").fetchone()["c"],
            0,
        )
        events = self.sales.list_events(actor=self.owner, lead_id=self.lead.id)
        self.assertEqual(
            [event["event_type"] for event in events].count("followup_owner_reminder"),
            1,
        )

    def test_wrong_tenant_cannot_read_or_mutate_followup(self) -> None:
        followup, _ = self._queue(request_key="tenant")
        other = self.tenancy.create_business(owner_user_id=902, name="Other")
        other_actor = self.tenancy.resolve_context(user_id=902, business_id=other.business.id)
        with self.assertRaisesRegex(ValueError, "not found"):
            self.followups.get(actor=other_actor, followup_id=followup.id)
        with self.assertRaisesRegex(Exception, "active business"):
            self.followups.cancel_active(actor=other_actor, lead_id=self.lead.id)

    def test_frequency_cap_blocks_fourth_customer_message(self) -> None:
        for index in range(3):
            followup, _ = self._queue(
                request_key=f"cap-{index}",
                scheduled_at=f"2026-08-{20 + index:02d}T10:00:00+00:00",
                now=f"2026-08-{20 + index:02d}T08:00:00+00:00",
            )
            self.conn.execute(
                "UPDATE clientplatform_sales_followups SET status='sent',sent_at=?,updated_at=? WHERE id=?",
                (followup.scheduled_at, followup.scheduled_at, followup.id),
            )
            self.conn.execute(
                "UPDATE provider_dispatch_outbox SET status='sent',sent_at=?,updated_at=? WHERE source_id=?",
                (followup.scheduled_at, followup.scheduled_at, followup.id),
            )
        with self.assertRaisesRegex(SalesInvariantViolation, "frequency cap"):
            self.followups.schedule(
                actor=self.owner,
                lead_id=self.lead.id,
                message_text="Четвёртое сообщение",
                scheduled_at="2026-08-23T10:00:00+00:00",
                request_key="cap-4",
                now="2026-08-23T08:00:00+00:00",
            )

    def test_non_messenger_source_is_owner_reminder_only(self) -> None:
        lead = self.sales.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key="web:u009",
            customer_id=self.customer.id,
            source_kind="website",
            contact_basis=ContactBasis.INBOUND,
            now="2026-08-19T08:00:00+00:00",
        )
        with self.assertRaisesRegex(SalesInvariantViolation, "original Telegram, VK or MAX"):
            self.followups.schedule(
                actor=self.owner,
                lead_id=lead.id,
                message_text="Напоминание",
                scheduled_at="2026-08-20T10:00:00+00:00",
                request_key="web-no-send",
                now="2026-08-20T08:00:00+00:00",
            )

    def test_non_replay_boundary_crash_is_quarantined_before_restart(self) -> None:
        followup, _ = self._queue(
            request_key="crash-boundary",
            scheduled_at="2026-08-20T10:00:00+00:00",
            now="2026-08-20T08:00:00+00:00",
        )
        claimed = self.outbox.claim_due(
            limit=5,
            lock_ttl_seconds=30,
            now=datetime.fromisoformat("2026-08-20T10:00:00+00:00"),
        )
        target = next(item for item in claimed if item.dispatch.source_id == followup.id)
        self.assertTrue(
            self.outbox.mark_sales_followup_non_replay_boundary(
                target,
                now="2026-08-20T10:00:01+00:00",
            )
        )
        replay = self.outbox.claim_due(
            limit=5,
            lock_ttl_seconds=30,
            now=datetime.fromisoformat("2026-08-20T10:00:31+00:00"),
        )
        self.assertFalse(any(item.dispatch.source_id == followup.id for item in replay))
        dispatch = self.conn.execute(
            "SELECT status,last_error FROM provider_dispatch_outbox WHERE source_id=?",
            (followup.id,),
        ).fetchone()
        self.assertEqual(dispatch["status"], "dead")
        self.assertIn("ambiguous", dispatch["last_error"])
        persisted = self.followups.get(actor=self.owner, followup_id=followup.id)
        self.assertEqual(persisted.status.value, "dead")
        events = self.sales.list_events(actor=self.owner, lead_id=self.lead.id)
        self.assertEqual(
            [event["event_type"] for event in events].count(
                "followup_delivery_ambiguous"
            ),
            1,
        )


    def test_provider_success_wins_audit_race_after_late_opt_out(self) -> None:
        followup, _ = self._queue(
            request_key="sent-race",
            scheduled_at="2026-08-20T10:00:00+00:00",
            now="2026-08-20T08:00:00+00:00",
        )
        claimed = self.outbox.claim_due(
            limit=5,
            now=datetime.fromisoformat("2026-08-20T10:00:00+00:00"),
        )
        target = next(item for item in claimed if item.dispatch.source_id == followup.id)
        self.followups.suppress_channel(
            actor=self.owner,
            lead_id=self.lead.id,
            now="2026-08-20T10:00:01+00:00",
        )
        self.outbox.mark_sent(
            target,
            provider_message_id="provider-accepted-1",
            now=datetime.fromisoformat("2026-08-20T10:00:02+00:00"),
        )
        persisted = self.followups.get(actor=self.owner, followup_id=followup.id)
        self.assertEqual(persisted.status.value, "sent")
        self.assertEqual(persisted.sent_at, "2026-08-20T10:00:02+00:00")
        with self.assertRaisesRegex(SalesInvariantViolation, "opted out"):
            self.followups.schedule(
                actor=self.owner,
                lead_id=self.lead.id,
                message_text="Нельзя отправлять после запрета",
                scheduled_at="2026-08-21T10:00:00+00:00",
                request_key="after-late-optout",
                now="2026-08-20T11:00:00+00:00",
            )


if __name__ == "__main__":
    unittest.main()

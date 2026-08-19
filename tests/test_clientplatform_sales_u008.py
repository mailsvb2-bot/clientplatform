from __future__ import annotations

import sqlite3
import unittest
from uuid import uuid4

from clientplatform.domain.sales import (
    ContactBasis,
    SalesInvariantViolation,
    SalesLeadNotFound,
    SalesLeadStage,
)
from clientplatform.domain.tenancy import PlatformRole
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from clientplatform.infrastructure.sales_ui_repository import SalesUiRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_attribution,
    clientplatform_customers,
    clientplatform_promotions,
    clientplatform_sales,
    clientplatform_tenancy,
)


class ClientPlatformSalesU008Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_sales.ensure(self.conn)
        clientplatform_promotions.ensure(self.conn)
        clientplatform_attribution.ensure(self.conn)

        self.tenancy = TenancyRepository(self.conn)
        access = self.tenancy.create_business(owner_user_id=101, name="Практика")
        self.owner = self.tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.customer = CustomerRepository(self.conn).create_customer(
            actor=self.owner,
            display_name="Анна",
        )
        self.sales = SalesRepository(self.conn)
        self.ui = SalesUiRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _lead(self, *, key: str = "web:anna"):
        return self.sales.create_or_refresh_lead(
            actor=self.owner,
            opportunity_key=key,
            customer_id=self.customer.id,
            source_kind="website",
            source_ref="landing-main",
            contact_basis=ContactBasis.INBOUND,
            now="2026-08-19T12:00:00+00:00",
        )

    def test_durable_next_action_due_is_normalized_and_projected(self) -> None:
        lead = self._lead()
        updated = self.sales.set_next_action(
            actor=self.owner,
            lead_id=lead.id,
            next_action="  Позвонить   клиенту  ",
            due_at="2026-08-20T10:00:00+03:00",
            now="2026-08-19T12:01:00+00:00",
        )

        self.assertEqual(updated.next_action, "Позвонить клиенту")
        self.assertEqual(updated.due_at, "2026-08-20T07:00:00+00:00")
        item = self.ui.list_open_work(actor=self.owner)[0]
        self.assertEqual(item["next_action"], "Позвонить клиенту")
        self.assertEqual(item["due_at"], "2026-08-20T07:00:00+00:00")
        self.assertEqual(item["source_ref"], "landing-main")

        events = self.sales.list_events(actor=self.owner, lead_id=lead.id)
        next_action_events = [
            event for event in events if event["event_type"] == "next_action_changed"
        ]
        self.assertEqual(len(next_action_events), 1)
        self.assertEqual(
            next_action_events[0]["payload"]["next_action"],
            "Позвонить клиенту",
        )

    def test_assignment_unassignment_is_tenant_scoped_and_audited(self) -> None:
        lead = self._lead()
        manager = self.tenancy.grant_member(
            actor=self.owner,
            user_id=102,
            role=PlatformRole.MANAGER,
        )

        assigned = self.sales.assign_member(
            actor=self.owner,
            lead_id=lead.id,
            member_id=manager.id,
            now="2026-08-19T12:02:00+00:00",
        )
        self.assertEqual(assigned.assigned_member_id, manager.id)
        projected = self.ui.list_open_work(actor=self.owner)[0]
        self.assertEqual(projected["assigned_member_id"], manager.id)
        self.assertEqual(projected["assigned_user_id"], 102)

        other_access = self.tenancy.create_business(owner_user_id=202, name="Другая")
        other_member_id = other_access.membership.id
        with self.assertRaisesRegex(ValueError, "active business"):
            self.sales.assign_member(
                actor=self.owner,
                lead_id=lead.id,
                member_id=other_member_id,
            )

        unassigned = self.sales.unassign_member(
            actor=self.owner,
            lead_id=lead.id,
            now="2026-08-19T12:03:00+00:00",
        )
        self.assertIsNone(unassigned.assigned_member_id)
        events = self.sales.list_events(actor=self.owner, lead_id=lead.id)
        self.assertEqual(
            [event["event_type"] for event in events].count("assignee_changed"),
            2,
        )

    def test_lost_reopen_won_lifecycle_clears_followup_and_normalizes_reason(self) -> None:
        lead = self._lead()
        lead = self.sales.set_next_action(
            actor=self.owner,
            lead_id=lead.id,
            next_action="Связаться завтра",
            due_at="2026-08-20T09:00:00+00:00",
        )
        lost = self.sales.set_stage(
            actor=self.owner,
            lead_id=lead.id,
            stage=SalesLeadStage.LOST,
            reason="  бюджет   заморожен  ",
            now="2026-08-19T12:04:00+00:00",
        )
        self.assertEqual(lost.closure_reason, "бюджет заморожен")
        self.assertIsNone(lost.next_action)
        self.assertIsNone(lost.due_at)
        self.assertEqual(self.ui.list_open_work(actor=self.owner), [])
        self.assertEqual(
            self.ui.list_recent_closed(actor=self.owner)[0]["closure_reason"],
            "бюджет заморожен",
        )

        reopened = self.sales.set_stage(
            actor=self.owner,
            lead_id=lead.id,
            stage=SalesLeadStage.NEW,
            reason="клиент вернулся",
            now="2026-08-19T12:05:00+00:00",
        )
        self.assertIsNone(reopened.closure_reason)
        self.assertEqual(reopened.stage, SalesLeadStage.NEW)

        won = self.sales.set_stage(
            actor=self.owner,
            lead_id=lead.id,
            stage=SalesLeadStage.WON,
            reason="  Оплата   получена  ",
            now="2026-08-19T12:06:00+00:00",
        )
        self.assertEqual(won.closure_reason, "Оплата получена")
        with self.assertRaisesRegex(SalesInvariantViolation, "cannot regress"):
            self.sales.set_stage(
                actor=self.owner,
                lead_id=lead.id,
                stage=SalesLeadStage.NEW,
            )

        stage_events = [
            event
            for event in self.sales.list_events(actor=self.owner, lead_id=lead.id)
            if event["event_type"] == "stage_changed"
        ]
        self.assertEqual(
            [event["payload"]["to_stage"] for event in stage_events],
            ["lost", "new", "won"],
        )

    def test_notes_use_existing_sales_events_and_dedupe(self) -> None:
        lead = self._lead()
        first = self.sales.add_note(
            actor=self.owner,
            lead_id=lead.id,
            note="  Клиент   попросил перезвонить после 18:00. ",
            dedupe_key="owner-note-42",
            now="2026-08-19T12:07:00+00:00",
        )
        replay = self.sales.add_note(
            actor=self.owner,
            lead_id=lead.id,
            note="другая версия не должна создать дубль",
            dedupe_key="owner-note-42",
            now="2026-08-19T12:08:00+00:00",
        )

        self.assertTrue(first)
        self.assertFalse(replay)
        notes = [
            event
            for event in self.sales.list_events(actor=self.owner, lead_id=lead.id)
            if event["event_type"] == "note_added"
        ]
        self.assertEqual(len(notes), 1)
        self.assertEqual(
            notes[0]["payload"]["note"],
            "Клиент попросил перезвонить после 18:00.",
        )
        self.assertEqual(
            notes[0]["payload"]["actor_member_id"],
            self.owner.membership_id,
        )
        table = self.conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='clientplatform_sales_notes'
            """
        ).fetchone()
        self.assertIsNone(table)

    def test_cross_tenant_mutations_fail_closed(self) -> None:
        lead = self._lead()
        other_access = self.tenancy.create_business(owner_user_id=202, name="Другая")
        other_owner = self.tenancy.resolve_context(
            user_id=202,
            business_id=other_access.business.id,
        )

        operations = (
            lambda: self.sales.get_lead(actor=other_owner, lead_id=lead.id),
            lambda: self.sales.set_next_action(
                actor=other_owner,
                lead_id=lead.id,
                next_action="Чужое действие",
            ),
            lambda: self.sales.set_stage(
                actor=other_owner,
                lead_id=lead.id,
                stage=SalesLeadStage.LOST,
                reason="чужая причина",
            ),
            lambda: self.sales.add_note(
                actor=other_owner,
                lead_id=lead.id,
                note="чужая заметка",
                dedupe_key="cross-tenant",
            ),
            lambda: self.sales.unassign_member(actor=other_owner, lead_id=lead.id),
        )
        for operation in operations:
            with self.assertRaises(SalesLeadNotFound):
                operation()

    def test_due_validation_and_closed_lead_guard_fail_closed(self) -> None:
        lead = self._lead()
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.sales.set_next_action(
                actor=self.owner,
                lead_id=lead.id,
                next_action="Позвонить",
                due_at="2026-08-20T10:00:00",
            )
        with self.assertRaisesRegex(SalesInvariantViolation, "requires"):
            self.sales.set_next_action(
                actor=self.owner,
                lead_id=lead.id,
                next_action=None,
                due_at="2026-08-20T10:00:00+00:00",
            )
        self.sales.set_stage(
            actor=self.owner,
            lead_id=lead.id,
            stage=SalesLeadStage.LOST,
            reason="нет потребности",
        )
        with self.assertRaisesRegex(SalesInvariantViolation, "closed"):
            self.sales.set_next_action(
                actor=self.owner,
                lead_id=lead.id,
                next_action="Нельзя",
            )

    def test_owner_projection_prefers_canonical_first_touch_and_support_cannot_read_it(self) -> None:
        lead = self._lead()
        identity_id = str(uuid4())
        touch_id = str(uuid4())
        link_id = str(uuid4())
        timestamp = "2026-08-19T11:00:00+00:00"
        self.conn.execute(
            """
            INSERT INTO attribution_identities(
                id, business_id, source, identity_kind, identity_fingerprint,
                source_ref_type, source_ref_id, promotion_campaign_id, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                identity_id,
                self.owner.business_id,
                "yandex_direct",
                "utm_fingerprint",
                "a" * 64,
                "creative_variant",
                "creative-42",
                None,
                timestamp,
            ),
        )
        self.conn.execute(
            """
            INSERT INTO acquisition_touches(
                id, business_id, attribution_identity_id, customer_id, source,
                occurred_at, metadata_json, metadata_version, created_at
            ) VALUES(?,?,?,?,?,?,?,1,?)
            """,
            (
                touch_id,
                self.owner.business_id,
                identity_id,
                self.customer.id,
                "yandex_direct",
                timestamp,
                "{}",
                timestamp,
            ),
        )
        self.conn.execute(
            """
            INSERT INTO attribution_links(
                id, business_id, touch_id, customer_id, booking_slot_id,
                model_version, created_at
            ) VALUES(?,?,?,?,NULL,'first_touch_v1',?)
            """,
            (
                link_id,
                self.owner.business_id,
                touch_id,
                self.customer.id,
                timestamp,
            ),
        )

        owner_item = self.ui.list_open_work(actor=self.owner)[0]
        self.assertEqual(owner_item["id"], lead.id)
        self.assertEqual(owner_item["attribution_source"], "yandex_direct")
        self.assertEqual(
            owner_item["attribution_source_ref_type"],
            "creative_variant",
        )
        self.assertEqual(owner_item["attribution_source_ref_id"], "creative-42")
        self.assertEqual(owner_item["attribution_model_version"], "first_touch_v1")

        self.tenancy.grant_member(
            actor=self.owner,
            user_id=103,
            role=PlatformRole.SUPPORT,
        )
        support = self.tenancy.resolve_context(
            user_id=103,
            business_id=self.owner.business_id,
        )
        support_item = self.ui.list_open_work(actor=support)[0]
        self.assertEqual(support_item["attribution_source"], "website")
        self.assertEqual(
            support_item["attribution_source_ref_id"],
            "landing-main",
        )
        self.assertIsNone(support_item["attribution_source_ref_type"])
        self.assertIsNone(support_item["attribution_model_version"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import services.db.core as db_core
from clientplatform.application import admin_ops
from clientplatform.application.bookings import create_booking_slot, list_booking_slots
from clientplatform.application.customers import create_customer
from clientplatform.application.activity import (
    archive_business_offering,
    create_business_offering,
    enable_business_capability,
    issue_customer_invite,
    list_business_offerings,
    save_business_profile,
)
from clientplatform.application.native_member_interactions import render_native_member_interaction
from clientplatform.application.owner_input import begin_owner_input, get_owner_input_session
from clientplatform.application.tenancy import (
    archive_business,
    create_business,
    grant_business_member,
    list_accessible_businesses,
    resolve_tenant_context,
    set_owner_control_workspace,
)
from clientplatform.domain.activity import ActivityNotFound, OfferingStatus
from clientplatform.domain.bookings import BookingNotFound
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import (
    BusinessStatus,
    PlatformRole,
    TenantPermissionDenied,
)
from clientplatform.infrastructure.booking_repository import BookingRepository
from services.db import get_db, get_db_ro
from services.schema import init_db


class ClientPlatformSafeRetirementUX294Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_db_path = db_core.DB_PATH
        self._tmpdir = tempfile.TemporaryDirectory(prefix="clientplatform-ux294-")
        db_core.DB_PATH = Path(self._tmpdir.name) / "ux294.db"
        init_db()

        access_a = create_business(owner_user_id=294001, name="Старый бизнес")
        access_b = create_business(owner_user_id=294002, name="Другой бизнес")
        self.owner_a = resolve_tenant_context(
            user_id=294001, business_id=access_a.business.id
        )
        self.owner_b = resolve_tenant_context(
            user_id=294002, business_id=access_b.business.id
        )
        save_business_profile(
            actor=self.owner_a,
            activity_description="Консультации и услуги",
            timezone_name="Europe/Moscow",
        )
        capability = enable_business_capability(
            actor=self.owner_a,
            connector_key="services",
        )
        self.offering = create_business_offering(
            actor=self.owner_a,
            capability_id=capability.id,
            title="Старая услуга",
            description="Больше не продаётся",
            idempotency_key="ux294-old-offer",
        )

    def tearDown(self) -> None:
        db_core.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    @staticmethod
    def _commands(message: object) -> set[str]:
        return {
            button.command
            for row in getattr(message, "rows", ())
            for button in row
        }

    def _render(self, raw_text: str, *, platform: ConnectionPlatform = ConnectionPlatform.TELEGRAM):
        return render_native_member_interaction(
            actor=self.owner_a,
            raw_text=raw_text,
            interaction_key=f"ux294:{platform.value}:{raw_text}",
            current_platform=platform,
        )

    def test_native_owner_ui_requires_confirmation_for_offering_and_publication_retirement(self) -> None:
        for platform in (
            ConnectionPlatform.TELEGRAM,
            ConnectionPlatform.VK,
            ConnectionPlatform.MAX,
        ):
            offers = self._render("cpm:offers", platform=platform)
            self.assertIn("cpm:offering-retire-list:0", self._commands(offers))

        retire_list = self._render("cpm:offering-retire-list:0")
        self.assertIn(
            f"cpm:offering-retire:{self.offering.id}",
            self._commands(retire_list),
        )
        confirm = self._render(f"cpm:offering-retire:{self.offering.id}")
        self.assertIn("историю", confirm.text.casefold())
        self.assertIn(
            f"cpm:offering-retire-ok:{self.offering.id}",
            self._commands(confirm),
        )

        publication = admin_ops.create_publication_draft(
            actor=self.owner_a,
            title="Устаревший пост",
            body="Нужно убрать из активной работы",
            channel="max",
            idempotency_key="ux294-native-post",
        )
        publications = self._render("cpm:publications")
        self.assertIn("cpm:publication-retire-list:0", self._commands(publications))
        publication_list = self._render("cpm:publication-retire-list:0")
        self.assertIn(
            f"cpm:publication-retire:{publication.id}",
            self._commands(publication_list),
        )
        publication_confirm = self._render(
            f"cpm:publication-retire:{publication.id}"
        )
        self.assertIn(
            f"cpm:publication-retire-ok:{publication.id}",
            self._commands(publication_confirm),
        )
        publication_result = self._render(
            f"cpm:publication-retire-ok:{publication.id}"
        )
        self.assertIn("убрана", publication_result.text.casefold())
        stale = self._render(f"cpm:publication-retire:{publication.id}")
        self.assertIn("неактуальна", stale.text.casefold())

    def test_native_business_retirement_confirmation_ends_dead_workspace_navigation(self) -> None:
        settings = self._render("cpm:manage-more")
        self.assertIn("cpm:business-retire", self._commands(settings))
        confirm = self._render("cpm:business-retire")
        self.assertIn("Оплаты, результаты и аудит не удаляются", confirm.text)
        self.assertIn("cpm:business-retire-ok", self._commands(confirm))

        result = self._render("cpm:business-retire-ok")
        self.assertIn("История сохранена", result.text)
        self.assertEqual((), result.rows)
        self.assertEqual([], list_accessible_businesses(user_id=self.owner_a.user_id))

    def test_offering_archive_is_idempotent_tenant_scoped_and_preserves_payment_history(self) -> None:
        payment = admin_ops.record_payment(
            actor=self.owner_a,
            amount_minor=500_00,
            currency="RUB",
            offering_id=self.offering.id,
            idempotency_key="ux294-payment",
            note="Историческая оплата",
        )

        archived = archive_business_offering(
            actor=self.owner_a,
            offering_id=self.offering.id,
        )
        replay = archive_business_offering(
            actor=self.owner_a,
            offering_id=self.offering.id,
        )
        self.assertEqual(OfferingStatus.ARCHIVED, archived.status)
        self.assertEqual(archived, replay)
        self.assertEqual([], list_business_offerings(
            actor=self.owner_a,
            capability_id=self.offering.capability_id,
        ))

        with self.assertRaises(ActivityNotFound):
            archive_business_offering(
                actor=self.owner_b,
                offering_id=self.offering.id,
            )

        with get_db_ro() as conn:
            payment_row = conn.execute(
                "SELECT id, status FROM business_payments WHERE id=?",
                (payment.id,),
            ).fetchone()
            evidence = conn.execute(
                "SELECT outcome_event_id FROM business_payment_outcome_evidence "
                "WHERE business_id=? AND payment_id=?",
                (self.owner_a.business_id, payment.id),
            ).fetchone()
            archive_audit = conn.execute(
                "SELECT COUNT(*) AS c FROM clientplatform_admin_audit_events "
                "WHERE business_id=? AND subject_id=? AND action='offering_archived'",
                (self.owner_a.business_id, self.offering.id),
            ).fetchone()["c"]
        self.assertIsNotNone(payment_row)
        self.assertEqual("paid", payment_row["status"])
        self.assertIsNotNone(evidence)
        self.assertEqual(1, archive_audit)

    def test_archived_offering_closes_open_booking_surface_and_stale_claim(self) -> None:
        customer = create_customer(actor=self.owner_a, display_name="Клиент")
        slot = create_booking_slot(
            actor=self.owner_a,
            offering_id=self.offering.id,
            local_start="20.09.2026 12:00",
            duration_minutes=60,
        )
        self.assertIn(slot.slot.id, {item.slot.id for item in list_booking_slots(actor=self.owner_a)})

        archive_business_offering(actor=self.owner_a, offering_id=self.offering.id)
        self.assertNotIn(
            slot.slot.id,
            {item.slot.id for item in list_booking_slots(actor=self.owner_a)},
        )
        self.assertIn(
            slot.slot.id,
            {
                item.slot.id
                for item in list_booking_slots(
                    actor=self.owner_a, include_unavailable=True
                )
            },
        )
        with get_db() as conn:
            with self.assertRaises(BookingNotFound):
                BookingRepository(conn).book_slot_for_customer_id(
                    business_id=self.owner_a.business_id,
                    customer_id=customer.id,
                    slot_id=slot.slot.id,
                    now="2026-09-05T15:30:00+00:00",
                )

    def test_publication_retirement_blocks_stale_actions_and_preserves_actual_history(self) -> None:
        scheduled = admin_ops.create_publication_draft(
            actor=self.owner_a,
            title="Старая акция",
            body="Этот пост больше не актуален",
            channel="vk",
            idempotency_key="ux294-scheduled-post",
        )
        with get_db() as conn:
            conn.execute(
                "UPDATE business_publications SET status='scheduled', scheduled_at=? "
                "WHERE id=? AND business_id=?",
                (
                    "2026-09-20T09:00:00+00:00",
                    scheduled.id,
                    self.owner_a.business_id,
                ),
            )

        retired = admin_ops.retire_publication(
            actor=self.owner_a,
            publication_id=scheduled.id,
        )
        replay = admin_ops.retire_publication(
            actor=self.owner_a,
            publication_id=scheduled.id,
        )
        self.assertEqual("cancelled", retired.status)
        self.assertEqual(retired.id, replay.id)
        self.assertNotIn(
            scheduled.id,
            {item.id for item in admin_ops.list_publications(actor=self.owner_a)},
        )
        self.assertIn(
            scheduled.id,
            {
                item.id
                for item in admin_ops.list_publications(
                    actor=self.owner_a,
                    include_retired=True,
                )
            },
        )
        projection = admin_ops.get_publication_calendar_projection(actor=self.owner_a)
        self.assertEqual(0, projection.scheduled_count)

        with self.assertRaises(ValueError):
            admin_ops.publish_publication(
                actor=self.owner_a,
                publication_id=scheduled.id,
            )
        with self.assertRaises(ValueError):
            admin_ops.schedule_publication(
                actor=self.owner_a,
                publication_id=scheduled.id,
                local_time="20.09.2026 12:00",
            )
        with self.assertRaises(ValueError):
            admin_ops.retire_publication(
                actor=self.owner_b,
                publication_id=scheduled.id,
            )

        published = admin_ops.create_publication_draft(
            actor=self.owner_a,
            title="Старая опубликованная запись",
            body="Исторический факт публикации должен сохраниться",
            channel="telegram",
            idempotency_key="ux294-published-post",
        )
        published = admin_ops.publish_publication(
            actor=self.owner_a,
            publication_id=published.id,
        )
        retired_published = admin_ops.retire_publication(
            actor=self.owner_a,
            publication_id=published.id,
        )
        self.assertEqual("published", retired_published.status)
        with get_db_ro() as conn:
            row = conn.execute(
                "SELECT status, published_at, retired_at FROM business_publications "
                "WHERE id=? AND business_id=?",
                (published.id, self.owner_a.business_id),
            ).fetchone()
            audit = conn.execute(
                "SELECT action FROM clientplatform_admin_audit_events "
                "WHERE business_id=? AND subject_id=? AND action='publication_retired'",
                (self.owner_a.business_id, published.id),
            ).fetchone()
        self.assertEqual("published", row["status"])
        self.assertIsNotNone(row["published_at"])
        self.assertIsNotNone(row["retired_at"])
        self.assertIsNotNone(audit)

    def test_business_archive_is_owner_only_cleans_transient_state_and_keeps_history(self) -> None:
        payment = admin_ops.record_payment(
            actor=self.owner_a,
            amount_minor=1000_00,
            currency="RUB",
            offering_id=self.offering.id,
            idempotency_key="ux294-business-payment",
        )
        invite = issue_customer_invite(actor=self.owner_a)
        set_owner_control_workspace(
            user_id=self.owner_a.user_id,
            platform="telegram",
            business_id=self.owner_a.business_id,
        )
        begin_owner_input(
            actor=self.owner_a,
            platform="telegram",
            action="activity_description",
        )
        admin_member = grant_business_member(
            actor=self.owner_a,
            user_id=294003,
            role=PlatformRole.ADMINISTRATOR,
        )
        admin_actor = resolve_tenant_context(
            user_id=admin_member.user_id,
            business_id=self.owner_a.business_id,
        )
        with self.assertRaises(TenantPermissionDenied):
            archive_business(actor=admin_actor)

        archived = archive_business(actor=self.owner_a)
        replay = archive_business(actor=self.owner_a)
        self.assertEqual(BusinessStatus.ARCHIVED, archived.status)
        self.assertEqual(archived, replay)
        self.assertEqual([], list_accessible_businesses(user_id=self.owner_a.user_id))
        self.assertIsNone(
            get_owner_input_session(
                user_id=self.owner_a.user_id,
                platform="telegram",
            )
        )

        with get_db_ro() as conn:
            business_row = conn.execute(
                "SELECT status FROM businesses WHERE id=?",
                (self.owner_a.business_id,),
            ).fetchone()
            workspace_count = conn.execute(
                "SELECT COUNT(*) AS c FROM clientplatform_owner_control_workspaces "
                "WHERE business_id=?",
                (self.owner_a.business_id,),
            ).fetchone()["c"]
            invite_row = conn.execute(
                "SELECT status, revoked_at FROM customer_invites WHERE id=?",
                (invite.invite.id,),
            ).fetchone()
            payment_row = conn.execute(
                "SELECT status FROM business_payments WHERE id=?",
                (payment.id,),
            ).fetchone()
            evidence_row = conn.execute(
                "SELECT outcome_event_id FROM business_payment_outcome_evidence "
                "WHERE business_id=? AND payment_id=?",
                (self.owner_a.business_id, payment.id),
            ).fetchone()
            audit_count = conn.execute(
                "SELECT COUNT(*) AS c FROM clientplatform_admin_audit_events "
                "WHERE business_id=?",
                (self.owner_a.business_id,),
            ).fetchone()["c"]
            business_archive_audit = conn.execute(
                "SELECT COUNT(*) AS c FROM clientplatform_admin_audit_events "
                "WHERE business_id=? AND action='business_archived' AND subject_id=?",
                (self.owner_a.business_id, self.owner_a.business_id),
            ).fetchone()["c"]
        self.assertEqual("archived", business_row["status"])
        self.assertEqual(0, workspace_count)
        self.assertEqual("revoked", invite_row["status"])
        self.assertIsNotNone(invite_row["revoked_at"])
        self.assertEqual("paid", payment_row["status"])
        self.assertIsNotNone(evidence_row)
        self.assertGreater(audit_count, 0)
        self.assertEqual(1, business_archive_audit)

        replacement = create_business(
            owner_user_id=self.owner_a.user_id,
            name="Новый бизнес",
        )
        active = list_accessible_businesses(user_id=self.owner_a.user_id)
        self.assertEqual([replacement.business.id], [item.business.id for item in active])


if __name__ == "__main__":
    unittest.main()

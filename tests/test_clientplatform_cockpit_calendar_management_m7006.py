from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.application import cockpit_calendar_management as management
from clientplatform.application import owner_booking_journey as owner_journey
from clientplatform.domain.activity import OfferingStatus
from clientplatform.domain.bookings import BookingInvariantViolation, BookingSlotStatus
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_bookings,
    clientplatform_customers,
    clientplatform_tenancy,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_MEMBER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _actor(role: PlatformRole = PlatformRole.OWNER) -> TenantContext:
    return TenantContext(
        business_id=_BUSINESS,
        user_id=101,
        membership_id=_MEMBER,
        role=role,
    )


class CockpitCalendarManagementM7006Tests(unittest.TestCase):
    def test_management_projects_only_active_canonical_offerings(self) -> None:
        actor = _actor()
        capabilities = [SimpleNamespace(id="cap-b"), SimpleNamespace(id="cap-a")]
        offerings = {
            "cap-b": [
                SimpleNamespace(id="off-2", title="  Диагностика  ", status=OfferingStatus.ACTIVE),
                SimpleNamespace(id="off-x", title="Архив", status=OfferingStatus.ARCHIVED),
            ],
            "cap-a": [SimpleNamespace(id="off-1", title="Консультация", status=OfferingStatus.ACTIVE)],
        }
        with (
            patch.object(management, "resolve_tenant_context", return_value=actor),
            patch.object(management, "get_business_profile", return_value=SimpleNamespace(timezone="Europe/Moscow")),
            patch.object(management, "list_business_capabilities", return_value=capabilities),
            patch.object(management, "list_business_offerings", side_effect=lambda *, capability_id, **_kwargs: offerings[capability_id]),
        ):
            snapshot = management.build_cockpit_calendar_management(
                actor=actor,
                business_name="Практика",
            )
        self.assertEqual(snapshot.business_id, _BUSINESS)
        self.assertEqual(snapshot.timezone_name, "Europe/Moscow")
        self.assertEqual(
            [(item.id, item.title) for item in snapshot.offerings],
            [("off-2", "  Диагностика  "), ("off-1", "Консультация")],
        )
        payload = snapshot.as_dict()
        self.assertNotIn("description", str(payload))
        self.assertNotIn("price", str(payload))

    def test_management_requires_both_calendar_read_and_program_management_permissions(self) -> None:
        for role in (PlatformRole.SUPPORT, PlatformRole.CONTENT_MANAGER):
            with self.subTest(role=role):
                actor = _actor(role)
                with (
                    patch.object(management, "resolve_tenant_context", return_value=actor),
                    patch.object(management, "get_business_profile") as profile,
                ):
                    with self.assertRaises(TenantPermissionDenied):
                        management.build_cockpit_calendar_management(
                            actor=actor,
                            business_name="Практика",
                        )
                profile.assert_not_called()

    def test_mutations_delegate_to_existing_booking_owners_after_live_recheck(self) -> None:
        actor = _actor(PlatformRole.MANAGER)
        context = SimpleNamespace(
            onboarding_required=False,
            business_id=_BUSINESS,
            business_name="Практика",
            user_id=101,
        )
        slot = SimpleNamespace(slot=SimpleNamespace(id="slot-1"))
        with (
            patch.object(management, "resolve_cockpit_context", return_value=context),
            patch.object(management, "resolve_tenant_context", return_value=actor),
            patch.object(management, "create_booking_slot", return_value=slot) as create,
            patch.object(management, "replace_owner_booking_slot", return_value=slot) as replace,
            patch.object(management, "cancel_owner_booking_slot", return_value=slot) as cancel,
        ):
            management.create_cockpit_calendar_slot(
                telegram_user_id=777,
                requested_business_id=_BUSINESS,
                offering_id="offering-1",
                local_start="10.09.2026 15:00",
                duration_minutes=60,
            )
            management.replace_cockpit_calendar_slot(
                telegram_user_id=777,
                requested_business_id=_BUSINESS,
                slot_id="slot-1",
                local_start="11.09.2026 16:00",
                duration_minutes=90,
            )
            management.cancel_cockpit_calendar_slot(
                telegram_user_id=777,
                requested_business_id=_BUSINESS,
                slot_id="slot-1",
            )
        create.assert_called_once_with(
            actor=actor,
            offering_id="offering-1",
            local_start="10.09.2026 15:00",
            duration_minutes=60,
        )
        replace.assert_called_once_with(
            actor=actor,
            slot_id="slot-1",
            local_start="11.09.2026 16:00",
            duration_minutes=90,
        )
        cancel.assert_called_once_with(actor=actor, slot_id="slot-1")

    def test_adapter_has_no_booking_repository_or_new_storage_owner(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "clientplatform" / "application" / "cockpit_calendar_management.py").read_text(encoding="utf-8")
        self.assertNotIn("BookingRepository", source)
        self.assertNotIn("clientplatform.infrastructure", source)
        self.assertIn("create_booking_slot", source)
        self.assertIn("replace_owner_booking_slot", source)
        self.assertIn("cancel_owner_booking_slot", source)

    def test_calendar_js_keeps_canonical_fallback_and_no_browser_authority_store(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "clientplatform" / "runtime" / "cockpit_calendar.js").read_text(encoding="utf-8")
        for endpoint in (
            "/clientplatform/cockpit/calendar/manage",
            "/clientplatform/cockpit/calendar/create",
            "/clientplatform/cockpit/calendar/replace",
            "/clientplatform/cockpit/calendar/cancel",
        ):
            self.assertIn(endpoint, script)
        self.assertIn('openCanonicalSection("calendar", advanced)', script)
        self.assertIn('if (management && item.status === "open")', script)
        self.assertIn("editingSlotId = null;\n      await load();", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("sessionStorage", script)
        self.assertNotIn("URLSearchParams", script)
        self.assertNotIn("innerHTML", script)


class CockpitCalendarCanonicalOwnerIntegrationM7006Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_bookings.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        activity = ActivityRepository(self.conn)
        self.bookings = BookingRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Практика")
        self.owner = tenancy.resolve_context(user_id=101, business_id=access.business.id)
        activity.upsert_profile(
            actor=self.owner,
            activity_description="Консультации",
            timezone_name="Europe/Tallinn",
            now="2026-09-06T10:00:00+00:00",
        )
        capability = activity.enable_capability(
            actor=self.owner,
            connector_key="consultations",
            now="2026-09-06T10:00:00+00:00",
        )
        self.offering = activity.create_offering(
            actor=self.owner,
            capability_id=capability.id,
            title="Консультация",
            description="60 минут",
            now="2026-09-06T10:00:00+00:00",
        )
        self.activity = activity
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    @contextmanager
    def _db(self):
        try:
            yield self.conn
        except (BookingInvariantViolation, ValueError, sqlite3.Error):
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def _open_slot(self, local_start: str):
        slot = self.bookings.create_slot(
            actor=self.owner,
            offering_id=self.offering.id,
            local_start=local_start,
            duration_minutes=60,
            now="2026-09-06T10:00:00+00:00",
        )
        self.conn.commit()
        return slot

    def _book(self, slot_id: str) -> None:
        issued = self.activity.issue_customer_invite(
            actor=self.owner,
            now="2026-09-06T10:01:00+00:00",
        )
        self.activity.claim_customer_invite(
            token=issued.token,
            telegram_user_id=700001,
            username="client",
            display_name="Клиент",
            now="2026-09-06T10:02:00+00:00",
        )
        self.bookings.book_slot(
            telegram_user_id=700001,
            business_id=self.owner.business_id,
            slot_id=slot_id,
            now="2026-09-06T10:03:00+00:00",
        )
        self.conn.commit()

    def test_owner_replace_and_cancel_keep_existing_canonical_lifecycle(self) -> None:
        slot = self._open_slot("10.09.2026 15:00")
        with (
            patch.object(owner_journey, "get_db", side_effect=self._db),
            patch("clientplatform.infrastructure.booking_repository._utc_now", return_value="2026-09-06T10:04:00+00:00"),
        ):
            replacement = owner_journey.replace_owner_booking_slot(
                actor=self.owner,
                slot_id=slot.slot.id,
                local_start="11.09.2026 16:00",
                duration_minutes=90,
            )
        old = self.bookings.get_slot(actor=self.owner, slot_id=slot.slot.id)
        self.assertEqual(old.slot.status, BookingSlotStatus.CANCELLED)
        self.assertEqual(replacement.slot.status, BookingSlotStatus.OPEN)
        self.assertEqual(replacement.local_start, "11.09.2026 16:00")
        self.assertEqual(replacement.slot.duration_minutes, 90)
        with patch.object(owner_journey, "get_db", side_effect=self._db):
            cancelled = owner_journey.cancel_owner_booking_slot(
                actor=self.owner,
                slot_id=replacement.slot.id,
            )
        self.assertEqual(cancelled.slot.status, BookingSlotStatus.CANCELLED)

    def test_booked_slot_cannot_be_silently_cancelled_or_replaced(self) -> None:
        slot = self._open_slot("12.09.2026 15:00")
        self._book(slot.slot.id)
        with patch.object(owner_journey, "get_db", side_effect=self._db):
            with self.assertRaises(BookingInvariantViolation):
                owner_journey.cancel_owner_booking_slot(actor=self.owner, slot_id=slot.slot.id)
            with self.assertRaises(BookingInvariantViolation):
                owner_journey.replace_owner_booking_slot(
                    actor=self.owner,
                    slot_id=slot.slot.id,
                    local_start="13.09.2026 15:00",
                    duration_minutes=60,
                )
        stored = self.bookings.get_slot(actor=self.owner, slot_id=slot.slot.id)
        self.assertEqual(stored.slot.status, BookingSlotStatus.BOOKED)


if __name__ == "__main__":
    unittest.main()

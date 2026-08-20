from __future__ import annotations

import sqlite3
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from clientplatform.application import promotions as promotion_app
from clientplatform.application.bookings import book_customer_slot_in_transaction
from clientplatform.domain.attribution import (
    AcquisitionSource,
    AttributionInvariantViolation,
)
from clientplatform.domain.promotions import (
    PromotionChannel,
    PromotionCreative,
    PromotionNotFound,
    stable_creative_id,
)
from clientplatform.domain.tenancy import PlatformRole, TenantPermissionDenied
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.attribution_repository import AttributionRepository
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.infrastructure.promotion_repository import PromotionRepository
from clientplatform.privacy_manifest import validate_clientplatform_privacy_manifest
from services.db.schema import (
    clientplatform_activity,
    clientplatform_attribution,
    clientplatform_bookings,
    clientplatform_customers,
    clientplatform_outcomes,
    clientplatform_promotions,
    clientplatform_tenancy,
)


_NOW = datetime(2026, 8, 15, 18, 0, tzinfo=timezone.utc)
_FUTURE_SLOT_LOCAL = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%d.%m.%Y 12:00")


class ClientPlatformAttributionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_bookings.ensure(self.conn)
        clientplatform_outcomes.ensure(self.conn)
        clientplatform_promotions.ensure(self.conn)
        clientplatform_attribution.ensure(self.conn)

        self.tenancy = TenancyRepository(self.conn)
        self.activity = ActivityRepository(self.conn)
        self.bookings = BookingRepository(self.conn)
        self.promotions = PromotionRepository(self.conn)
        self.attribution = AttributionRepository(self.conn)

        self.business = self.tenancy.create_business(owner_user_id=101, name="Сантехник")
        self.owner = self.tenancy.resolve_context(
            user_id=101,
            business_id=self.business.business.id,
        )
        self.activity.upsert_profile(
            actor=self.owner,
            activity_description="Ремонт сантехники",
            timezone_name="Europe/Amsterdam",
            now="2026-08-01T10:00:00+00:00",
        )
        capability = self.activity.enable_capability(
            actor=self.owner,
            connector_key="services",
            now="2026-08-01T10:00:00+00:00",
        )
        self.offering = self.activity.create_offering(
            actor=self.owner,
            capability_id=capability.id,
            title="Замена раковины",
            description="Установка раковины",
            now="2026-08-01T10:00:00+00:00",
        )
        self.slot = self.bookings.create_slot(
            actor=self.owner,
            offering_id=self.offering.id,
            local_start=_FUTURE_SLOT_LOCAL,
            duration_minutes=60,
            now="2026-08-01T10:00:00+00:00",
        )
        creative = PromotionCreative(
            creative_id=stable_creative_id("sink", "attribution"),
            headline="Замена раковины",
            primary_text="Свободное время 20 августа. Запись онлайн.",
            description="60 минут",
        )
        self.telegram_campaign, _ = self.promotions.create_or_refresh_campaign(
            actor=self.owner,
            slot_id=self.slot.slot.id,
            channel=PromotionChannel.TELEGRAM,
            creative=creative,
            now="2026-08-01T10:10:00+00:00",
        )
        self.vk_campaign, _ = self.promotions.create_or_refresh_campaign(
            actor=self.owner,
            slot_id=self.slot.slot.id,
            channel=PromotionChannel.VK,
            creative=creative,
            now="2026-08-01T10:11:00+00:00",
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _connect_customer(self, telegram_user_id: int) -> str:
        issued = self.activity.issue_customer_invite(
            actor=self.owner,
            now="2026-08-01T10:00:00+00:00",
        )
        claim = self.activity.claim_customer_invite(
            token=issued.token,
            telegram_user_id=telegram_user_id,
            username=f"customer{telegram_user_id}",
            display_name="Клиент",
            now="2026-08-01T10:05:00+00:00",
        )
        return claim.customer_id

    def _capture(self, *, campaign, customer_id: str, source_kind: str = "campaign"):
        return self.attribution.capture_promotion_touch(
            business_id=self.business.business.id,
            source_token=campaign.source_token,
            campaign_id=campaign.id,
            channel=campaign.channel,
            source_kind=source_kind,
            source_key=campaign.id,
            customer_id=customer_id,
            occurred_at=_NOW,
        )

    def test_duplicate_tap_is_idempotent_and_raw_token_is_never_stored(self) -> None:
        customer_id = self._connect_customer(700001)

        first = self._capture(campaign=self.telegram_campaign, customer_id=customer_id)
        second = self._capture(campaign=self.telegram_campaign, customer_id=customer_id)

        self.assertEqual(second.identity.id, first.identity.id)
        self.assertEqual(second.touch.id, first.touch.id)
        self.assertEqual(second.link.id, first.link.id)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM attribution_identities").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM acquisition_touches").fetchone()[0],
            1,
        )
        serialized = "\n".join(
            str(value)
            for row in self.conn.execute(
                "SELECT identity_fingerprint, source_ref_type, source_ref_id FROM attribution_identities"
            ).fetchall()
            for value in row
        )
        self.assertNotIn(self.telegram_campaign.source_token, serialized)

    def test_later_touch_does_not_rewrite_customer_first_touch(self) -> None:
        customer_id = self._connect_customer(700002)
        first = self._capture(campaign=self.telegram_campaign, customer_id=customer_id)
        later = self._capture(campaign=self.vk_campaign, customer_id=customer_id)

        self.assertEqual(later.touch.id, first.touch.id)
        self.assertEqual(later.identity.id, first.identity.id)
        self.assertEqual(later.identity.source, AcquisitionSource.TELEGRAM)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM acquisition_touches").fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM attribution_links WHERE customer_id=?",
                (customer_id,),
            ).fetchone()[0],
            1,
        )

    def test_explicit_yandex_source_survives_telegram_delivery_channel(self) -> None:
        customer_id = self._connect_customer(700003)
        trace = self._capture(
            campaign=self.telegram_campaign,
            customer_id=customer_id,
            source_kind="yandex_direct",
        )
        self.assertEqual(trace.identity.source, AcquisitionSource.YANDEX_DIRECT)
        self.assertEqual(trace.touch.source, AcquisitionSource.YANDEX_DIRECT)

    def test_cross_tenant_customer_reference_is_rejected_by_database(self) -> None:
        other = self.tenancy.create_business(owner_user_id=202, name="Другой бизнес")
        other_owner = self.tenancy.resolve_context(
            user_id=202,
            business_id=other.business.id,
        )
        other_activity = ActivityRepository(self.conn)
        other_activity.upsert_profile(
            actor=other_owner,
            activity_description="Другой бизнес",
            timezone_name="Europe/Amsterdam",
            now="2026-08-01T10:00:00+00:00",
        )
        invite = other_activity.issue_customer_invite(
            actor=other_owner,
            now="2026-08-01T10:00:00+00:00",
        )
        other_customer = other_activity.claim_customer_invite(
            token=invite.token,
            telegram_user_id=800001,
            username="other",
            display_name="Другой клиент",
            now="2026-08-01T10:05:00+00:00",
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self._capture(
                campaign=self.telegram_campaign,
                customer_id=other_customer.customer_id,
            )

    def test_booking_inherits_customer_first_touch_and_rejects_wrong_customer(self) -> None:
        attributed_customer = self._connect_customer(700004)
        other_customer = self._connect_customer(700005)
        first = self._capture(
            campaign=self.telegram_campaign,
            customer_id=attributed_customer,
        )
        claim = self.bookings.book_slot(
            telegram_user_id=700005,
            business_id=self.business.business.id,
            slot_id=self.slot.slot.id,
            now="2026-08-15T18:05:00+00:00",
        )
        self.assertEqual(claim.customer_id, other_customer)

        with self.assertRaises(AttributionInvariantViolation):
            self.attribution.link_booking_from_customer(
                business_id=self.business.business.id,
                customer_id=attributed_customer,
                booking_slot_id=self.slot.slot.id,
                created_at=_NOW,
            )

        other_trace = self._capture(
            campaign=self.vk_campaign,
            customer_id=other_customer,
        )
        booking_trace = self.attribution.link_booking_from_customer(
            business_id=self.business.business.id,
            customer_id=other_customer,
            booking_slot_id=self.slot.slot.id,
            created_at=_NOW,
        )
        self.assertIsNotNone(booking_trace)
        assert booking_trace is not None
        self.assertEqual(booking_trace.touch.id, other_trace.touch.id)
        self.assertEqual(booking_trace.link.booking_slot_id, self.slot.slot.id)
        self.assertNotEqual(booking_trace.touch.id, first.touch.id)

    def test_canonical_booking_path_inherits_existing_first_touch(self) -> None:
        customer_id = self._connect_customer(700008)
        customer_trace = self._capture(
            campaign=self.telegram_campaign,
            customer_id=customer_id,
        )

        claim = book_customer_slot_in_transaction(
            self.conn,
            telegram_user_id=700008,
            business_id=self.business.business.id,
            slot_id=self.slot.slot.id,
        )

        self.assertEqual(claim.customer_id, customer_id)
        booking_trace = self.attribution.get_booking_trace(
            business_id=self.business.business.id,
            booking_slot_id=self.slot.slot.id,
        )
        self.assertIsNotNone(booking_trace)
        assert booking_trace is not None
        self.assertEqual(booking_trace.touch.id, customer_trace.touch.id)
        self.assertEqual(booking_trace.identity.id, customer_trace.identity.id)

    def test_promoted_booking_writes_outcome_and_attribution_end_to_end(self) -> None:
        customer_id = self._connect_customer(700006)
        with patch.object(
            promotion_app,
            "get_db",
            side_effect=lambda: nullcontext(self.conn),
        ):
            claim, campaign = promotion_app.book_promoted_slot(
                source_token=self.telegram_campaign.source_token,
                telegram_user_id=700006,
            )

        self.assertEqual(claim.customer_id, customer_id)
        self.assertEqual(campaign.id, self.telegram_campaign.id)
        customer_trace = self.attribution.get_customer_trace(
            business_id=self.business.business.id,
            customer_id=customer_id,
        )
        booking_trace = self.attribution.get_booking_trace(
            business_id=self.business.business.id,
            booking_slot_id=self.slot.slot.id,
        )
        self.assertIsNotNone(customer_trace)
        self.assertIsNotNone(booking_trace)
        assert customer_trace is not None and booking_trace is not None
        self.assertEqual(customer_trace.touch.id, booking_trace.touch.id)
        self.assertEqual(customer_trace.identity.source, AcquisitionSource.TELEGRAM)
        outcome = self.conn.execute(
            """
            SELECT outcome_type, customer_id, source_id
            FROM business_outcome_events
            WHERE business_id=? AND idempotency_key=?
            """,
            (
                self.business.business.id,
                f"booking_created:{self.slot.slot.id}",
            ),
        ).fetchone()
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome["outcome_type"], "booking_created")
        self.assertEqual(outcome["customer_id"], customer_id)
        self.assertEqual(outcome["source_id"], self.slot.slot.id)

    def test_forged_valid_shape_token_fails_closed_without_attribution(self) -> None:
        self._connect_customer(700007)
        with patch.object(
            promotion_app,
            "get_db",
            side_effect=lambda: nullcontext(self.conn),
        ):
            with self.assertRaises(PromotionNotFound):
                promotion_app.book_promoted_slot(
                    source_token="abcdefghijkl",
                    telegram_user_id=700007,
                )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM acquisition_touches").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM attribution_links").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM business_outcome_events").fetchone()[0],
            0,
        )

    def test_raw_attribution_permission_is_strict(self) -> None:
        self.owner.assert_can_view_attribution_spine()
        self.tenancy.grant_member(
            actor=self.owner,
            user_id=303,
            role=PlatformRole.MARKETER,
            now="2026-08-01T10:00:00+00:00",
        )
        marketer = self.tenancy.resolve_context(
            user_id=303,
            business_id=self.business.business.id,
        )
        with self.assertRaises(TenantPermissionDenied):
            marketer.assert_can_view_attribution_spine()

    def test_privacy_manifest_covers_attribution_tables(self) -> None:
        report = validate_clientplatform_privacy_manifest(
            self.conn,
            require_complete=False,
        )
        self.assertTrue(report.ok)
        self.assertIn("attribution_identities", report.discovered_business_tables)
        self.assertIn("acquisition_touches", report.discovered_business_tables)
        self.assertIn("attribution_links", report.discovered_business_tables)


if __name__ == "__main__":
    unittest.main()

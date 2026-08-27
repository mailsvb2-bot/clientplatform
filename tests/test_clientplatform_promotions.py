from __future__ import annotations

import sqlite3
import unittest

from clientplatform.application.promotion_creatives import (
    PromotionBrief,
    generate_promotion_candidates,
    select_promotion_creative,
)
from clientplatform.domain.promotions import (
    CreativeGuardrails,
    PromotionChannel,
    PromotionCreative,
    PromotionEventType,
    PromotionInvariantViolation,
    PromotionNotFound,
    stable_creative_id,
    validate_creative,
)
from clientplatform.domain.tenancy import PlatformRole, TenantPermissionDenied
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.infrastructure.promotion_repository import PromotionRepository
from clientplatform.privacy_manifest import validate_clientplatform_privacy_manifest
from services.db.schema import (
    clientplatform_activity,
    clientplatform_bookings,
    clientplatform_customers,
    clientplatform_promotions,
    clientplatform_tenancy,
)


class PromotionCreativeTests(unittest.TestCase):
    def test_stable_ids_are_deterministic_and_input_sensitive(self) -> None:
        self.assertEqual(stable_creative_id("a", "b"), stable_creative_id("a", "b"))
        self.assertNotEqual(stable_creative_id("a", "b"), stable_creative_id("a", "c"))
        self.assertRegex(stable_creative_id("a"), r"^cr_[0-9a-f]{16}$")

    def test_local_service_and_consultation_receive_different_safe_copy(self) -> None:
        plumber = PromotionBrief(
            business_name="Вася-сантехник",
            activity_description="Ремонтирую сантехнику и устанавливаю раковины",
            offering_title="Замена раковины",
            offering_description="Сниму старую и установлю новую раковину",
            local_start="10.08.2026 12:00",
            duration_minutes=60,
        )
        psychologist = PromotionBrief(
            business_name="Психолог Маша",
            activity_description="Провожу психологические консультации для взрослых",
            offering_title="Первая консультация",
            offering_description="Знакомство и обсуждение запроса",
            local_start="11.08.2026 18:00",
            duration_minutes=60,
        )

        plumber_copy = select_promotion_creative(generate_promotion_candidates(plumber))
        psychologist_copy = select_promotion_creative(
            generate_promotion_candidates(psychologist)
        )

        self.assertIn("раков", plumber_copy.primary_text.lower())
        self.assertTrue(
            any(
                marker in psychologist_copy.primary_text.lower()
                for marker in ("встреч", "консультац", "формат")
            )
        )
        self.assertNotEqual(plumber_copy.creative_id, psychologist_copy.creative_id)
        self.assertTrue(validate_creative(plumber_copy)[0])
        self.assertTrue(validate_creative(psychologist_copy)[0])

    def test_guardrails_reject_guarantees_shaming_and_medical_claims(self) -> None:
        unsafe = [
            PromotionCreative(
                creative_id=stable_creative_id("guarantee"),
                headline="Лучший в мире",
                primary_text="100% гарантия результата",
                description="Запись",
            ),
            PromotionCreative(
                creative_id=stable_creative_id("shaming"),
                headline="Запись",
                primary_text="Тебе должно быть стыдно, если не придёшь",
                description="Консультация",
            ),
            PromotionCreative(
                creative_id=stable_creative_id("medical"),
                headline="Консультация",
                primary_text="Вылечим тревогу и избавим навсегда",
                description="Запись",
            ),
        ]
        reasons = [validate_creative(item, CreativeGuardrails())[1] for item in unsafe]
        self.assertEqual(reasons, ["deny_phrase", "shaming_language", "medical_claims"])

    def test_selection_rejects_empty_candidate_list(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires candidates"):
            select_promotion_creative([])


class ClientPlatformPromotionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_bookings.ensure(self.conn)
        clientplatform_promotions.ensure(self.conn)
        self.tenancy = TenancyRepository(self.conn)
        self.activity = ActivityRepository(self.conn)
        self.bookings = BookingRepository(self.conn)
        self.promotions = PromotionRepository(self.conn)
        self.business = self.tenancy.create_business(
            owner_user_id=101,
            name="Сантехник",
        )
        self.owner = self.tenancy.resolve_context(
            user_id=101,
            business_id=self.business.business.id,
        )
        self.activity.upsert_profile(
            actor=self.owner,
            activity_description="Ремонтирую сантехнику",
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
            description="Сниму старую и установлю новую раковину",
            now="2026-08-01T10:00:00+00:00",
        )
        self.slot = self.bookings.create_slot(
            actor=self.owner,
            offering_id=self.offering.id,
            local_start="10.08.2026 12:00",
            duration_minutes=60,
            now="2026-08-01T10:00:00+00:00",
        )
        self.creative = PromotionCreative(
            creative_id=stable_creative_id("sink", "telegram"),
            headline="Замена раковины",
            primary_text="Доступно время 10 августа. Запишитесь онлайн.",
            description="Сантехник · 60 минут",
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _connect_customer(self, telegram_user_id: int = 700001) -> str:
        issued = self.activity.issue_customer_invite(
            actor=self.owner,
            now="2026-08-01T10:00:00+00:00",
        )
        claim = self.activity.claim_customer_invite(
            token=issued.token,
            telegram_user_id=telegram_user_id,
            username="customer",
            display_name="Клиент",
            now="2026-08-01T10:05:00+00:00",
        )
        return claim.customer_id

    def test_promotion_roles_list_slots_without_customer_record_access(self) -> None:
        for user_id, role in (
            (202, PlatformRole.MARKETER),
            (203, PlatformRole.CONTENT_MANAGER),
        ):
            with self.subTest(role=role.value):
                self.tenancy.grant_member(
                    actor=self.owner,
                    user_id=user_id,
                    role=role,
                    now="2026-08-01T10:00:00+00:00",
                )
                actor = self.tenancy.resolve_context(
                    user_id=user_id,
                    business_id=self.business.business.id,
                )
                with self.assertRaises(TenantPermissionDenied):
                    self.bookings.list_slots(
                        actor=actor,
                        now="2026-08-01T10:00:00+00:00",
                    )
                promotable = self.promotions.list_promotable_slots(
                    actor=actor,
                    now="2026-08-01T10:00:00+00:00",
                )
                self.assertEqual(
                    [item.slot.id for item in promotable],
                    [self.slot.slot.id],
                )

    def test_campaign_refresh_preserves_source_link_and_separates_channels(self) -> None:
        telegram, _ = self.promotions.create_or_refresh_campaign(
            actor=self.owner,
            slot_id=self.slot.slot.id,
            channel=PromotionChannel.TELEGRAM,
            creative=self.creative,
            now="2026-08-01T10:10:00+00:00",
        )
        refreshed_copy = PromotionCreative(
            creative_id=stable_creative_id("sink", "telegram", "v2"),
            headline="Свободное время сантехника",
            primary_text="Замена раковины 10 августа. Прямая запись онлайн.",
            description="60 минут",
        )
        refreshed, _ = self.promotions.create_or_refresh_campaign(
            actor=self.owner,
            slot_id=self.slot.slot.id,
            channel=PromotionChannel.TELEGRAM,
            creative=refreshed_copy,
            now="2026-08-01T10:11:00+00:00",
        )
        vk, _ = self.promotions.create_or_refresh_campaign(
            actor=self.owner,
            slot_id=self.slot.slot.id,
            channel=PromotionChannel.VK,
            creative=self.creative,
            now="2026-08-01T10:12:00+00:00",
        )

        self.assertEqual(telegram.id, refreshed.id)
        self.assertEqual(telegram.source_token, refreshed.source_token)
        self.assertEqual(refreshed.creative.headline, "Свободное время сантехника")
        self.assertNotEqual(vk.source_token, telegram.source_token)
        self.assertEqual(len(self.promotions.list_campaigns(actor=self.owner)), 2)

    def test_max_campaign_persists_through_repository(self) -> None:
        campaign, _ = self.promotions.create_or_refresh_campaign(
            actor=self.owner,
            slot_id=self.slot.slot.id,
            channel=PromotionChannel.MAX,
            creative=self.creative,
            now="2026-08-01T10:10:00+00:00",
        )

        self.assertEqual(campaign.channel, PromotionChannel.MAX)
        stored = self.conn.execute(
            "SELECT channel FROM promotion_campaigns WHERE id=? AND business_id=?",
            (campaign.id, self.business.business.id),
        ).fetchone()
        self.assertIsNotNone(stored)
        self.assertEqual(str(stored["channel"]), "max")

    def test_expired_open_slot_is_not_promotable_or_public(self) -> None:
        campaign, _ = self.promotions.create_or_refresh_campaign(
            actor=self.owner,
            slot_id=self.slot.slot.id,
            channel=PromotionChannel.TELEGRAM,
            creative=self.creative,
            now="2026-08-01T10:10:00+00:00",
        )
        expired_at = "2026-08-10T10:00:00+00:00"

        self.assertEqual(
            self.promotions.list_promotable_slots(
                actor=self.owner,
                now=expired_at,
            ),
            [],
        )
        with self.assertRaises(PromotionNotFound):
            self.promotions.get_public_campaign(
                source_token=campaign.source_token,
                now=expired_at,
            )
        with self.assertRaises(PromotionInvariantViolation):
            self.promotions.create_or_refresh_campaign(
                actor=self.owner,
                slot_id=self.slot.slot.id,
                channel=PromotionChannel.VK,
                creative=self.creative,
                now=expired_at,
            )

    def test_unique_opens_booking_attribution_and_stale_link_closure(self) -> None:
        campaign, _ = self.promotions.create_or_refresh_campaign(
            actor=self.owner,
            slot_id=self.slot.slot.id,
            channel=PromotionChannel.TELEGRAM,
            creative=self.creative,
            now="2026-08-01T10:10:00+00:00",
        )
        customer_id = self._connect_customer()

        self.assertTrue(
            self.promotions.record_event(
                campaign=campaign,
                customer_id=customer_id,
                event_type=PromotionEventType.OPENED,
                now="2026-08-01T10:20:00+00:00",
            )
        )
        self.assertFalse(
            self.promotions.record_event(
                campaign=campaign,
                customer_id=customer_id,
                event_type=PromotionEventType.OPENED,
                now="2026-08-01T10:21:00+00:00",
            )
        )
        self.bookings.book_slot(
            telegram_user_id=700001,
            business_id=self.business.business.id,
            slot_id=self.slot.slot.id,
            now="2026-08-01T10:22:00+00:00",
        )
        self.assertTrue(
            self.promotions.record_event(
                campaign=campaign,
                customer_id=customer_id,
                event_type=PromotionEventType.BOOKED,
                now="2026-08-01T10:22:00+00:00",
            )
        )

        stats = self.promotions.stats(actor=self.owner)
        self.assertEqual(stats.campaigns, 1)
        self.assertEqual(stats.people_opened, 1)
        self.assertEqual(stats.bookings, 1)
        self.assertEqual(stats.conversion_percent, 100.0)
        with self.assertRaises(PromotionNotFound):
            self.promotions.get_public_campaign(
                source_token=campaign.source_token,
                now="2026-08-01T10:23:00+00:00",
            )

    def test_public_link_closes_when_slot_is_booked_by_another_path(self) -> None:
        campaign, _ = self.promotions.create_or_refresh_campaign(
            actor=self.owner,
            slot_id=self.slot.slot.id,
            channel=PromotionChannel.WEBSITE,
            creative=self.creative,
            now="2026-08-01T10:10:00+00:00",
        )
        self._connect_customer()
        self.bookings.book_slot(
            telegram_user_id=700001,
            business_id=self.business.business.id,
            slot_id=self.slot.slot.id,
            now="2026-08-01T10:22:00+00:00",
        )
        with self.assertRaises(PromotionNotFound):
            self.promotions.get_public_campaign(
                source_token=campaign.source_token,
                now="2026-08-01T10:23:00+00:00",
            )

    def test_privacy_manifest_covers_promotion_tables(self) -> None:
        report = validate_clientplatform_privacy_manifest(
            self.conn,
            require_complete=False,
        )
        self.assertTrue(report.ok)
        self.assertIn("promotion_campaigns", report.discovered_business_tables)
        self.assertIn("promotion_events", report.discovered_business_tables)


if __name__ == "__main__":
    unittest.main()

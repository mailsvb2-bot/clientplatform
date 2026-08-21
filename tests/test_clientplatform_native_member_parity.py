from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application import native_member_interactions as ui
from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.bookings import BookingSlotStatus
from clientplatform.domain.customers import CustomerPlatform
from clientplatform.domain.tenancy import PlatformRole, TenantContext


def _actor(role: PlatformRole, *, user_id: int = 101) -> TenantContext:
    return TenantContext(
        business_id=str(uuid4()),
        membership_id=str(uuid4()),
        user_id=user_id,
        role=role,
    )


def _commands(message) -> list[str]:
    return [button.command for row in message.rows for button in row]


class NativeMemberParityNavigationTests(unittest.TestCase):
    def test_owner_home_exposes_all_native_sections_within_button_limit(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        with patch.object(ui, "_business_name", return_value="Практика"):
            message = ui._menu_message(actor, linked=False)
        self.assertEqual(
            ["cpm:work", "cpm:growth", "cpm:manage", "cpm:team"],
            _commands(message),
        )
        self.assertLessEqual(sum(len(row) for row in message.rows), 10)

    def test_support_home_does_not_expose_growth_management_or_team(self) -> None:
        actor = _actor(PlatformRole.SUPPORT)
        with patch.object(ui, "_business_name", return_value="Практика"):
            message = ui._menu_message(actor, linked=False)
        self.assertEqual(["cpm:work"], _commands(message))

    def test_work_section_contains_telegram_admin_operational_reads(self) -> None:
        message = ui._work_message(_actor(PlatformRole.OWNER))
        commands = set(_commands(message))
        self.assertTrue(
            {
                "cpm:today",
                "cpm:today-full",
                "cpm:customers:0",
                "cpm:bookings",
                "cpm:programs",
                "cpm:behavior",
                "cpm:attention",
            }.issubset(commands)
        )
        self.assertLessEqual(sum(len(row) for row in message.rows), 10)

    def test_forged_management_and_team_commands_fail_closed_by_role(self) -> None:
        support = _actor(PlatformRole.SUPPORT)
        for action in ("manage", "team", "release", "formats", "members", "permissions"):
            with self.subTest(action=action):
                message = ui._render(
                    support,
                    ui.ParsedMemberInteraction(action),
                    linked=False,
                    setup_issuer=None,
                    setup_key="test",
                )
                self.assertIn("недоступен", message.text.casefold())
                self.assertEqual(["cpm:menu"], _commands(message))

    def test_parser_preserves_pagination_and_entity_arguments(self) -> None:
        parsed = ui.parse_native_member_interaction("cpm:customers:7")
        self.assertEqual("customers", parsed.action)
        self.assertEqual(("7",), parsed.args)
        customer_id = str(uuid4())
        parsed = ui.parse_native_member_interaction(f"cpm:customer:{customer_id}")
        self.assertEqual((customer_id,), parsed.args)


class NativeMemberParityReadTests(unittest.TestCase):
    def test_detailed_today_reuses_canonical_business_services(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        summary = SimpleNamespace(
            customers=12,
            programs=3,
            dispatch_pending=2,
            dispatch_sent=18,
            dispatch_attention=1,
        )
        profile = SimpleNamespace(status=SimpleNamespace(value="ready"))
        capabilities = [SimpleNamespace(status=CapabilityStatus.ACTIVE)]
        slots = [SimpleNamespace(slot=SimpleNamespace(status=BookingSlotStatus.OPEN))]
        progress = [SimpleNamespace(completed_lessons=2, total_lessons=4)]
        with (
            patch.object(ui, "get_business_profile", return_value=profile),
            patch.object(ui, "business_delivery_summary", return_value=summary),
            patch.object(ui, "list_business_capabilities", return_value=capabilities),
            patch.object(ui, "list_booking_slots", return_value=slots),
            patch.object(ui, "list_business_program_progress", return_value=progress),
            patch.object(ui, "_business_name", return_value="Практика"),
        ):
            message = ui._today_full_message(actor)
        self.assertIn("Клиентов: 12", message.text)
        self.assertIn("Прохождение материалов: 2/4", message.text)
        self.assertIn("Свободных времён: 1", message.text)

    def test_customer_list_is_paginated_and_buttons_open_tenant_customer_card(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        customers = [
            SimpleNamespace(id=str(uuid4()), display_name=f"Клиент {index}")
            for index in range(9)
        ]
        with patch.object(ui, "list_customers", return_value=customers):
            first = ui._customers_message(actor, 0)
            second = ui._customers_message(actor, 1)
        self.assertIn("cpm:customers:1", _commands(first))
        self.assertTrue(any(command.startswith("cpm:customer:") for command in _commands(first)))
        self.assertIn("cpm:customers:0", _commands(second))
        self.assertLessEqual(sum(len(row) for row in first.rows), 10)
        self.assertLessEqual(sum(len(row) for row in second.rows), 10)

    def test_customer_card_uses_canonical_customer_record(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        customer_id = str(uuid4())
        record = SimpleNamespace(
            customer=SimpleNamespace(
                display_name="Анна",
                status=SimpleNamespace(value="active"),
                created_at="2026-08-21T00:00:00+00:00",
            ),
            identities=[
                SimpleNamespace(
                    platform=CustomerPlatform.VK,
                    username="anna_vk",
                    display_name="Анна",
                    external_subject="700001",
                )
            ],
        )
        with patch.object(ui, "get_customer", return_value=record) as getter:
            message = ui._customer_message(actor, customer_id)
        getter.assert_called_once_with(actor=actor, customer_id=customer_id)
        self.assertIn("Анна", message.text)
        self.assertIn("vk: @anna_vk", message.text)

    def test_owner_team_pagination_stays_inside_native_transport_limit(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        members = [
            {"user_id": 1000 + index, "role": "manager", "status": "active"}
            for index in range(9)
        ]
        with patch.object(ui, "_list_members", return_value=members):
            first = ui._members_message(actor, 0)
            second = ui._members_message(actor, 1)
        self.assertIn("cpm:members:1", _commands(first))
        self.assertIn("cpm:members:0", _commands(second))
        self.assertLessEqual(sum(len(row) for row in first.rows), 10)
        self.assertLessEqual(sum(len(row) for row in second.rows), 10)

    def test_release_report_matches_canonical_read_state(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        profile = SimpleNamespace(status=SimpleNamespace(value="ready"))
        summary = SimpleNamespace(
            customers=5,
            programs=2,
            dispatch_pending=0,
            dispatch_sent=7,
            dispatch_attention=0,
        )
        capabilities = [SimpleNamespace(status=CapabilityStatus.ACTIVE)]
        with (
            patch.object(ui, "get_business_profile", return_value=profile),
            patch.object(ui, "business_delivery_summary", return_value=summary),
            patch.object(ui, "list_business_capabilities", return_value=capabilities),
            patch.object(ui, "list_booking_slots", return_value=[]),
            patch.object(ui, "list_customers", return_value=[]),
            patch.object(ui, "list_programs", return_value=[]),
            patch.object(ui, "list_business_program_progress", return_value=[]),
        ):
            message = ui._admin_report_message(actor, "release")
        self.assertIn("Итог: ГОТОВО", message.text)
        self.assertIn("Ошибки отправки: ✅", message.text)


if __name__ == "__main__":
    unittest.main()

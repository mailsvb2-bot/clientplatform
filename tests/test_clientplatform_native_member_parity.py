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
    def test_owner_home_has_one_primary_action_and_preserves_all_sections_behind_more(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        primary = ui._button("🚀 Найти новых клиентов", "cpm:acquire")
        with (
            patch.object(ui, "_business_name", return_value="Практика"),
            patch.object(ui, "_native_primary_action", return_value=primary),
        ):
            message = ui._menu_message(actor, linked=False)
        self.assertEqual(["cpm:acquire", "cpm:menu-all"], _commands(message))
        self.assertEqual(sum(len(row) for row in message.rows), 2)

        advanced = ui._menu_all_message(actor)
        commands = _commands(advanced)
        self.assertTrue(
            {"cpm:work", "cpm:messengers", "cpm:growth", "cpm:manage", "cpm:team"}.issubset(commands)
        )
        self.assertIn("cpm:menu", commands)

    def test_support_home_has_one_primary_action_without_exposing_management(self) -> None:
        actor = _actor(PlatformRole.SUPPORT)
        primary = ui._button("📊 Проверить, что происходит", "cpm:today")
        with (
            patch.object(ui, "_business_name", return_value="Практика"),
            patch.object(ui, "_native_primary_action", return_value=primary),
        ):
            message = ui._menu_message(actor, linked=False)
        self.assertEqual(["cpm:today", "cpm:menu-all"], _commands(message))
        self.assertEqual(["cpm:work", "cpm:messengers", "cpm:menu"], _commands(ui._menu_all_message(actor)))

    def test_native_home_falls_back_to_role_safe_manual_read_when_cockpit_is_unavailable(self) -> None:
        marketer = _actor(PlatformRole.MARKETER)
        with patch.object(
            ui,
            "get_growth_cockpit",
            side_effect=ui.TenantPermissionDenied("not available for this projection"),
        ):
            primary = ui._native_primary_action(marketer)
        self.assertEqual(primary.command, "cpm:growth")
        self.assertIn("вручную", primary.label.casefold())

        owner = _actor(PlatformRole.OWNER)
        with patch.object(
            ui,
            "get_growth_cockpit",
            side_effect=RuntimeError("projection temporarily unavailable"),
        ):
            primary = ui._native_primary_action(owner)
        self.assertEqual(primary.command, "cpm:today")
        self.assertIn("вручную", primary.label.casefold())

    def test_native_primary_action_opens_durable_manual_sales_step_directly(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        lead_id = str(uuid4())
        snapshot = SimpleNamespace(
            next_action=SimpleNamespace(action_key=f"sales_lead:{lead_id}")
        )
        with patch.object(ui, "get_growth_cockpit", return_value=snapshot):
            primary = ui._native_primary_action(actor)
        self.assertEqual(primary.command, f"cpm:sales-lead:{lead_id}")
        self.assertIn("клиент", primary.label.casefold())

    def test_native_today_shows_bounded_operating_queue_and_one_primary_action(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        lead_id = str(uuid4())
        action = SimpleNamespace(
            title="Следующий шаг по клиенту: Анна",
            reason="Сохранён следующий шаг: позвонить.",
            action_key=f"sales_lead:{lead_id}",
        )
        journey = SimpleNamespace(
            leads=11,
            bookings=5,
            completed_bookings=4,
            paid_customers=3,
            reactivated_customers=2,
            verified_revenue_by_currency=(
                SimpleNamespace(amount_minor=34_200_00, currency="RUB"),
            ),
            attributed_revenue_by_currency=(
                SimpleNamespace(amount_minor=27_900_00, currency="RUB"),
            ),
            unattributed_revenue_by_currency=(
                SimpleNamespace(amount_minor=6_300_00, currency="RUB"),
            ),
            sources=(
                SimpleNamespace(
                    source=SimpleNamespace(value="vk"),
                    revenue_by_currency=(
                        SimpleNamespace(amount_minor=34_200_00, currency="RUB"),
                    ),
                    paid_customers=3,
                ),
            ),
        )
        snapshot = SimpleNamespace(
            actions=(action,),
            next_action=action,
            journey=journey,
            period_days=7,
        )
        summary = SimpleNamespace(
            customers=4,
            programs=2,
            dispatch_pending=1,
            dispatch_sent=7,
            dispatch_attention=0,
        )
        with (
            patch.object(ui, "business_delivery_summary", return_value=summary),
            patch.object(ui, "get_growth_cockpit", return_value=snapshot),
        ):
            message = ui._today_message(actor)
        self.assertIn("Важные действия", message.text)
        self.assertIn("Следующий шаг по клиенту: Анна", message.text)
        self.assertIn("Деньги и путь клиента · 7 дней", message.text)
        self.assertIn("Подтверждённая выручка: 34 200.00 RUB", message.text)
        self.assertIn("Связано с источником: 27 900.00 RUB", message.text)
        self.assertIn("Без подтверждённого источника: 6 300.00 RUB", message.text)
        self.assertIn("Лучший подтверждённый источник: ВКонтакте", message.text)
        commands = _commands(message)
        self.assertEqual(commands[0], f"cpm:sales-lead:{lead_id}")
        self.assertEqual(sum(command.startswith("cpm:sales-lead:") for command in commands), 1)
        self.assertLessEqual(sum(len(row) for row in message.rows), 10)

    def test_native_money_and_source_labels_match_canonical_semantics(self) -> None:
        money = ui._native_money_text(
            (
                SimpleNamespace(amount_minor=500, currency="JPY"),
                SimpleNamespace(amount_minor=1234, currency="KWD"),
            ),
            empty="нет",
        )
        self.assertEqual(money, "500 JPY, 1.234 KWD")

        for source, label in (
            ("organic", "Органика"),
            ("partner", "Партнёры"),
            ("manual_import", "Импорт / вручную"),
        ):
            journey = SimpleNamespace(
                leads=1,
                bookings=1,
                completed_bookings=1,
                paid_customers=1,
                reactivated_customers=0,
                verified_revenue_by_currency=(SimpleNamespace(amount_minor=500, currency="JPY"),),
                attributed_revenue_by_currency=(SimpleNamespace(amount_minor=500, currency="JPY"),),
                unattributed_revenue_by_currency=(),
                sources=(
                    SimpleNamespace(
                        source=SimpleNamespace(value=source),
                        revenue_by_currency=(SimpleNamespace(amount_minor=500, currency="JPY"),),
                        paid_customers=1,
                    ),
                ),
            )
            text = ui._native_journey_text(SimpleNamespace(journey=journey, period_days=7))
            self.assertIn("Связано с источником: 500 JPY", text)
            self.assertIn(f"Лучший подтверждённый источник: {label}", text)
            self.assertNotIn(f": {source}", text)

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

    def test_forged_sales_progressive_disclosure_commands_fail_closed_by_role(self) -> None:
        actor = _actor(PlatformRole.CONTENT_MANAGER)
        lead_id = str(uuid4())
        for action in ("sales-actions", "sales-result-menu"):
            with self.subTest(action=action):
                message = ui._render(
                    actor,
                    ui.ParsedMemberInteraction(action, (lead_id,)),
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
        timeline = SimpleNamespace(entries=())
        with (
            patch.object(ui, "get_customer", return_value=record) as getter,
            patch.object(ui, "get_customer_timeline", return_value=timeline) as timeline_getter,
            patch.object(
                ui,
                "format_customer_timeline_lines",
                return_value=("• 27.08.2026 · Получена оплата · 500,00 RUB",),
            ),
        ):
            message = ui._customer_message(actor, customer_id)
        getter.assert_called_once_with(actor=actor, customer_id=customer_id)
        timeline_getter.assert_called_once_with(actor=actor, customer_id=customer_id)
        self.assertIn("Анна", message.text)
        self.assertIn("vk: @anna_vk", message.text)
        self.assertIn("История клиента", message.text)
        self.assertIn("Получена оплата", message.text)

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

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application import native_member_interactions as ui
from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.tenancy import PlatformRole, TenantContext

ROOT = Path(__file__).resolve().parents[1]


def _actor(role: PlatformRole = PlatformRole.OWNER) -> TenantContext:
    return TenantContext(
        business_id=str(uuid4()),
        membership_id=str(uuid4()),
        user_id=101,
        role=role,
    )


def _commands(message) -> list[str]:
    return [button.command for row in message.rows for button in row]


def _action_literals(path: Path, function_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    )
    result: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name):
            continue
        if node.left.id != "action":
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                result.add(comparator.value)
            elif isinstance(comparator, (ast.Set, ast.Tuple, ast.List)):
                result.update(
                    item.value
                    for item in comparator.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                )
    return result


class NativeFullParityContractTests(unittest.TestCase):
    def test_every_reachable_telegram_admin_action_has_native_semantic_equivalent(self) -> None:
        actions = _action_literals(ROOT / "handlers/clientplatform_admin.py", "admin_gate")
        actions |= _action_literals(
            ROOT / "handlers/clientplatform_admin_extension.py",
            "admin_ops_gate",
        )
        transport_only = {
            "menu",
            "back",
            "leave",
            "return-publications",
            "return-payments",
            "return-prices",
        }
        missing = (actions - transport_only) - set(ui.TELEGRAM_NATIVE_ACTION_EQUIVALENTS)
        self.assertEqual(set(), missing)

    def test_every_literal_native_button_route_is_admitted_by_parser(self) -> None:
        source = (ROOT / "clientplatform/application/native_member_interactions.py").read_text(encoding="utf-8")
        actions = set(re.findall(r'_button\([^\n]*?[f]?["](?:[^"\n]*?)cpm:([a-z0-9_-]+)', source))
        self.assertTrue(actions)
        for action in sorted(actions):
            with self.subTest(action=action):
                parsed = ui.parse_native_member_interaction(f"cpm:{action}")
                self.assertEqual(action, parsed.action)

    def test_all_native_equivalent_actions_are_admitted_by_parser(self) -> None:
        for telegram_action, native_actions in ui.TELEGRAM_NATIVE_ACTION_EQUIVALENTS.items():
            for native_action in native_actions:
                with self.subTest(telegram=telegram_action, native=native_action):
                    parsed = ui.parse_native_member_interaction(f"cpm:{native_action}")
                    self.assertEqual(native_action, parsed.action)

    def test_simple_owner_intent_registry_is_fully_admitted_by_native_parser(self) -> None:
        for intent, native_actions in ui.SIMPLE_OWNER_NATIVE_INTENT_EQUIVALENTS.items():
            for native_action in native_actions:
                with self.subTest(intent=intent, native=native_action):
                    parsed = ui.parse_native_member_interaction(f"cpm:{native_action}")
                    self.assertEqual(native_action, parsed.action)

    def test_native_text_inputs_cover_telegram_fsm_mutations(self) -> None:
        cases = {
            "черновик VK | Заголовок | Полный текст": (
                "publication-new-text",
                ("vk", "Заголовок", "Полный текст"),
            ),
            "оплата 3500 RUB - - | консультация": (
                "payment-new-text",
                ("3500", "RUB", "-", "-", "консультация"),
            ),
            "цена abcdef12 5000 RUB": (
                "price-set-text",
                ("abcdef12", "5000", "RUB"),
            ),
            "сотрудник 12345 support": (
                "member-add-text",
                ("12345", "support"),
            ),
            "деятельность Психологическая практика для взрослых": (
                "activity-edit-text",
                ("Психологическая практика для взрослых",),
            ),
            "программа Курс спокойного сна": (
                "program-create-text",
                ("Курс спокойного сна",),
            ),
            "урок abcdef12 текст | Введение | Первый шаг": (
                "program-lesson-text",
                ("abcdef12", "text", "Введение", "Первый шаг"),
            ),
            "выдать abcdef12 12345678": (
                "program-deliver-text",
                ("abcdef12", "12345678"),
            ),
            "предложение consultations | Консультация 60 минут | Личная встреча": (
                "offering-new-text",
                ("consultations", "Консультация 60 минут", "Личная встреча"),
            ),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                parsed = ui.parse_native_member_interaction(text)
                self.assertEqual(expected, (parsed.action, parsed.args))

    def test_progressive_disclosure_preserves_owner_growth_surface_within_transport_limit(self) -> None:
        actor = _actor()
        first = ui._growth_message(actor)
        sales = ui._growth_sales_message(actor)
        analysis = ui._growth_analysis_message(actor)
        second = ui._growth_more_message(actor)
        lifecycle = ui._growth_lifecycle_message(actor)
        commands = set(
            _commands(first)
            + _commands(sales)
            + _commands(analysis)
            + _commands(second)
            + _commands(lifecycle)
        )
        expected = {
            "cpm:acquire",
            "cpm:autopilot",
            "cpm:publications",
            "cpm:money",
            "cpm:payments",
            "cpm:segments",
            "cpm:offers",
            "cpm:copy",
            "cpm:prices",
            "cpm:invites",
            "cpm:funnel2",
            "cpm:funnel",
            "cpm:retention",
        }
        self.assertTrue(expected.issubset(commands))
        for message in (first, sales, analysis, second, lifecycle):
            self.assertLessEqual(len(message.rows), 6)
            self.assertTrue(all(len(row) == 1 for row in message.rows))


    def test_middle_pages_never_exceed_native_button_ceiling(self) -> None:
        actor = _actor()
        customers = [
            SimpleNamespace(id=str(uuid4()), display_name=f"Клиент {index}")
            for index in range(15)
        ]
        members = [
            {"user_id": 1000 + index, "role": "manager", "status": "active"}
            for index in range(15)
        ]
        with patch.object(ui, "list_customers", return_value=customers):
            customer_middle = ui._customers_message(actor, 1)
        with patch.object(ui, "_list_members", return_value=members):
            member_middle = ui._members_message(actor, 1)
        self.assertLessEqual(sum(len(row) for row in customer_middle.rows), 10)
        self.assertLessEqual(sum(len(row) for row in member_middle.rows), 10)
        self.assertIn("cpm:customers:0", _commands(customer_middle))
        self.assertIn("cpm:customers:2", _commands(customer_middle))
        self.assertIn("cpm:members:0", _commands(member_middle))
        self.assertIn("cpm:members:2", _commands(member_middle))

    def test_marketer_does_not_gain_admin_only_invites(self) -> None:
        message = ui._growth_more_message(_actor(PlatformRole.MARKETER))
        self.assertNotIn("cpm:invites", _commands(message))


class NativeFullParityMutationTests(unittest.TestCase):
    def test_publication_create_and_publish_use_canonical_admin_ops(self) -> None:
        actor = _actor(PlatformRole.CONTENT_MANAGER)
        publication = SimpleNamespace(id=str(uuid4()), title="Новость")
        with patch.object(ui.admin_ops, "create_publication_draft", return_value=publication) as create:
            message = ui._publication_new_result(
                actor, "vk", "Новость", "Текст", interaction_key="event-publication"
            )
        create.assert_called_once_with(
            actor=actor,
            title="Новость",
            body="Текст",
            channel="vk",
            idempotency_key="event-publication:publication-create",
        )
        self.assertIn("Черновик", message.text)

        with patch.object(ui.admin_ops, "publish_publication", return_value=publication) as publish:
            message = ui._publication_publish_result(actor, publication.id)
        publish.assert_called_once_with(actor=actor, publication_id=publication.id)
        self.assertIn("опубликованной", message.text)

    def test_manual_payment_uses_same_canonical_fact_and_native_event_idempotency(self) -> None:
        actor = _actor(PlatformRole.MANAGER)
        payment = SimpleNamespace(
            amount_minor=350000,
            currency="RUB",
            outcome_event_id=str(uuid4()),
        )
        with (
            patch.object(ui, "list_customers", return_value=[]),
            patch.object(ui, "_native_all_offerings", return_value=[]),
            patch.object(ui.admin_ops, "record_payment", return_value=payment) as record,
        ):
            message = ui._payment_new_result(
                actor,
                "3500",
                "RUB",
                "-",
                "-",
                "консультация",
                interaction_key="route:event:42",
            )
        kwargs = record.call_args.kwargs
        self.assertEqual(350000, kwargs["amount_minor"])
        self.assertEqual("RUB", kwargs["currency"])
        self.assertIsNone(kwargs["customer_id"])
        self.assertIsNone(kwargs["offering_id"])
        self.assertTrue(kwargs["idempotency_key"].startswith("native-payment:"))
        self.assertIn("Канонический факт выручки подтверждён", message.text)

    def test_refund_is_confirmed_then_written_through_canonical_admin_ops(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        payment_id = str(uuid4())
        payment = SimpleNamespace(
            id=payment_id,
            status="paid",
            outcome_event_id=str(uuid4()),
            amount_minor=500000,
            currency="RUB",
        )
        with patch.object(ui.admin_ops, "list_payments", return_value=[payment]):
            confirm = ui._payment_refund_confirm(actor, payment_id)
        self.assertIn(f"cpm:pay-refund-ok:{payment_id}", _commands(confirm))
        with patch.object(ui.admin_ops, "refund_payment", return_value=payment) as refund:
            result = ui._payment_refund_result(actor, payment_id)
        self.assertEqual(payment_id, refund.call_args.kwargs["payment_id"])
        self.assertIn("Возврат", result.text)

    def test_native_money_scaling_uses_iso4217_currency_exponent(self) -> None:
        self.assertEqual(3500, ui._native_amount_minor("3500", "JPY"))
        self.assertEqual(3_500_000, ui._native_amount_minor("3500", "KWD"))
        self.assertEqual(350_000, ui._native_amount_minor("3500", "RUB"))
        self.assertEqual("3 500 JPY", ui._native_amount_label(3500, "JPY"))
        self.assertEqual("3 500.000 KWD", ui._native_amount_label(3_500_000, "KWD"))

    def test_price_mutation_resolves_offering_and_uses_canonical_price_api(self) -> None:
        actor = _actor(PlatformRole.MARKETER)
        offering_id = str(uuid4())
        offering = SimpleNamespace(id=offering_id, title="Консультация")
        price = SimpleNamespace(offering_title="Консультация", amount_minor=500000, currency="RUB")
        with (
            patch.object(ui, "_native_all_offerings", return_value=[offering]),
            patch.object(ui.admin_ops, "set_offering_price", return_value=price) as setter,
        ):
            result = ui._price_set_result(actor, offering_id[:8], "5000", "RUB")
        self.assertEqual(offering_id, setter.call_args.kwargs["offering_id"])
        self.assertEqual(500000, setter.call_args.kwargs["amount_minor"])
        self.assertIn("5 000.00 RUB", result.text)

    def test_invite_is_universal_across_telegram_vk_and_max(self) -> None:
        actor = _actor()
        issued = SimpleNamespace(token="token123")
        with patch.object(ui, "issue_customer_invite", return_value=issued):
            result = ui._invite_new_result(actor)
        self.assertIn("cpj_token123", result.text)
        self.assertIn("Telegram, ВКонтакте или MAX", result.text)

    def test_automation_owner_controls_use_canonical_policy_boundary(self) -> None:
        actor = _actor()
        with patch.object(ui.admin_ops, "set_autopilot_enabled", return_value=True) as setter:
            result = ui._automation_mutation_message(actor, "autopilot-enable")
        setter.assert_called_once_with(actor=actor, enabled=True)
        self.assertIn("включён", result.text)

        with patch.object(ui.admin_ops, "set_autopilot_enabled", return_value=False) as setter:
            result = ui._automation_mutation_message(actor, "autopilot-disable")
        setter.assert_called_once_with(actor=actor, enabled=False)
        self.assertIn("выключен", result.text)

        approval_id = str(uuid4())
        with patch.object(ui.admin_ops, "approve_pending_automation_action") as approve:
            result = ui._automation_mutation_message(actor, "automation-approve", approval_id)
        approve.assert_called_once_with(actor=actor, approval_id=approval_id)
        self.assertIn("не запущено", result.text)

    def test_formats_are_editable_with_same_canonical_capability_api(self) -> None:
        actor = _actor(PlatformRole.ADMINISTRATOR)
        capability = SimpleNamespace(
            connector_key="consultations",
            title="Консультации",
            status=CapabilityStatus.DISABLED,
        )
        changed = SimpleNamespace(title="Консультации")
        with (
            patch.object(ui, "list_business_capabilities", return_value=[capability]),
            patch.object(ui, "enable_business_capability", return_value=changed) as enable,
        ):
            result = ui._format_toggle_result(actor, "consultations", enabled=True)
        enable.assert_called_once_with(actor=actor, connector_key="consultations", title="Консультации")
        self.assertIn("включён", result.text)

    def test_owner_can_add_change_and_revoke_member_using_canonical_tenancy_api(self) -> None:
        actor = _actor()
        member = SimpleNamespace(user_id=202, role=PlatformRole.SUPPORT)
        with patch.object(ui, "grant_business_member", return_value=member) as grant:
            added = ui._member_add_result(actor, "202", "support")
        grant.assert_called_once_with(actor=actor, user_id=202, role=PlatformRole.SUPPORT)
        self.assertIn("добавлен", added.text)

        member = SimpleNamespace(user_id=202, role=PlatformRole.MANAGER)
        with patch.object(ui, "grant_business_member", return_value=member) as grant:
            changed = ui._member_role_result(actor, 202, "manager")
        grant.assert_called_once_with(actor=actor, user_id=202, role=PlatformRole.MANAGER)
        self.assertIn("Менеджер", changed.text)

        with patch.object(ui, "revoke_business_member", return_value=member) as revoke:
            revoked = ui._member_revoke_result(actor, 202)
        revoke.assert_called_once_with(actor=actor, user_id=202)
        self.assertIn("отозван", revoked.text)

    def test_activity_edit_preserves_timezone_and_respects_business_management_roles(self) -> None:
        actor = _actor(PlatformRole.ADMINISTRATOR)
        current = SimpleNamespace(activity_description="Старое", timezone="Europe/Moscow")
        updated = SimpleNamespace(activity_description="Новое", timezone="Europe/Moscow")
        with (
            patch.object(ui, "get_business_profile", return_value=current),
            patch.object(ui, "save_business_profile", return_value=updated) as save,
        ):
            result = ui._activity_edit_result(actor, "Новое")
        save.assert_called_once_with(
            actor=actor,
            activity_description="Новое",
            timezone_name="Europe/Moscow",
        )
        self.assertIn("Новое", result.text)

    def test_program_workflow_uses_canonical_program_and_native_delivery_boundaries(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        program_id = str(uuid4())
        customer_id = str(uuid4())
        draft = SimpleNamespace(id=program_id, title="Курс", status=SimpleNamespace(value="draft"))
        lesson = SimpleNamespace(title="Введение")

        with patch.object(ui, "create_program", return_value=draft) as create:
            created = ui._program_create_result(
                actor, "Курс", interaction_key="event-program"
            )
        create.assert_called_once_with(
            actor=actor,
            title="Курс",
            idempotency_key="event-program:program-create",
        )
        self.assertIn("Черновик", created.text)

        with (
            patch.object(ui, "list_programs", return_value=[draft]),
            patch.object(ui, "add_program_lesson", return_value=lesson) as add_lesson,
        ):
            added = ui._program_lesson_result(
                actor,
                program_id[:8],
                "text",
                "Введение",
                "Первый шаг",
                interaction_key="event-lesson",
            )
        add_lesson.assert_called_once_with(
            actor=actor,
            program_id=program_id,
            title="Введение",
            content_kind="text",
            content_ref="Первый шаг",
            idempotency_key="event-lesson:program-lesson",
        )
        self.assertIn("добавлен", added.text)

        record = SimpleNamespace(
            program=SimpleNamespace(id=program_id, title="Курс"),
            lessons=(lesson,),
        )
        active = SimpleNamespace(id=program_id, title="Курс", status=SimpleNamespace(value="active"))
        with (
            patch.object(ui, "get_program_draft", return_value=record),
            patch.object(ui, "publish_program", return_value=active) as publish,
        ):
            published = ui._program_publish_result(
                actor, program_id, current_platform=ui.ConnectionPlatform.VK
            )
        publish.assert_called_once_with(actor=actor, program_id=program_id)
        self.assertIn(f"cpm:program-deliver:{program_id}", _commands(published))

        customer = SimpleNamespace(id=customer_id, display_name="Анна")
        prepared = SimpleNamespace(program=SimpleNamespace(program=SimpleNamespace(title="Курс")))
        with (
            patch.object(ui, "list_programs", return_value=[active]),
            patch.object(ui, "list_customers_with_active_identity", return_value=[customer]) as eligible,
            patch.object(ui, "prepare_native_program_delivery", return_value=prepared) as deliver,
        ):
            result = ui._program_deliver_result(
                actor,
                program_id[:8],
                customer_id[:8],
                current_platform=ui.ConnectionPlatform.VK,
            )
        eligible.assert_called_once_with(actor=actor, platform="vk", limit=100)
        deliver.assert_called_once_with(
            actor=actor,
            program_id=program_id,
            customer_id=customer_id,
            platform=ui.ConnectionPlatform.VK,
        )
        self.assertIn("ВКонтакте", result.text)

    def test_program_delivery_help_lists_only_customers_reachable_in_current_native_channel(self) -> None:
        actor = _actor(PlatformRole.MANAGER)
        program_id = str(uuid4())
        customer = SimpleNamespace(id=str(uuid4()), display_name="Клиент MAX")
        with patch.object(
            ui,
            "list_customers_with_active_identity",
            return_value=[customer],
        ) as eligible:
            message = ui._program_deliver_help(
                actor,
                program_id,
                current_platform=ui.ConnectionPlatform.MAX,
            )
        eligible.assert_called_once_with(actor=actor, platform="max", limit=8)
        self.assertIn("Клиент MAX", message.text)
        self.assertIn("доступные в MAX", message.text)

    def test_offering_creation_uses_canonical_activity_boundary(self) -> None:
        actor = _actor(PlatformRole.CONTENT_MANAGER)
        capability = SimpleNamespace(
            id=str(uuid4()),
            connector_key="consultations",
            title="Консультации",
            status=CapabilityStatus.ACTIVE,
        )
        offering = SimpleNamespace(title="Консультация 60 минут")
        with (
            patch.object(ui, "list_business_capabilities", return_value=[capability]),
            patch.object(ui, "create_business_offering", return_value=offering) as create,
        ):
            result = ui._offering_new_result(
                actor,
                "consultations",
                "Консультация 60 минут",
                "Личная встреча",
                interaction_key="event-offering",
            )
        create.assert_called_once_with(
            actor=actor,
            capability_id=capability.id,
            title="Консультация 60 минут",
            description="Личная встреча",
            idempotency_key="event-offering:offering-create",
        )
        self.assertIn("создано", result.text)

    def test_program_middle_page_stays_within_native_button_ceiling(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        programs = [
            SimpleNamespace(
                id=str(uuid4()),
                title=f"Черновик {index}",
                status=SimpleNamespace(value="draft"),
            )
            for index in range(9)
        ]
        with patch.object(ui, "list_programs", return_value=programs):
            message = ui._programs_message(actor, 1)
        self.assertLessEqual(sum(len(row) for row in message.rows), 10)
        self.assertIn("cpm:programs:0", _commands(message))
        self.assertIn("cpm:programs:2", _commands(message))

    def test_beginner_form_buttons_start_canonical_owner_input_sessions(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        offering_id = str(uuid4())
        program_id = str(uuid4())
        with patch.object(ui, "begin_owner_input") as begin:
            ui._publication_new_for_message(
                actor, "vk", current_platform=ui.ConnectionPlatform.VK
            )
            begin.assert_called_with(
                actor=actor,
                platform="vk",
                action="publication_draft",
                context={"channel": "vk"},
                surface="official",
            )

            begin.reset_mock()
            with patch.object(
                ui,
                "_native_all_offerings",
                return_value=[SimpleNamespace(id=offering_id, title="Консультация")],
            ):
                ui._price_set_help(
                    actor, offering_id, current_platform=ui.ConnectionPlatform.MAX
                )
            begin.assert_called_with(
                actor=actor,
                platform="max",
                action="price",
                context={"offering_id": offering_id},
                surface="official",
            )

            begin.reset_mock()
            with patch.object(
                ui,
                "get_program_draft",
                return_value=SimpleNamespace(
                    program=SimpleNamespace(id=program_id, title="Курс")
                ),
            ):
                ui._program_lesson_kind_message(
                    actor,
                    program_id,
                    "text",
                    current_platform=ui.ConnectionPlatform.VK,
                )
            begin.assert_called_with(
                actor=actor,
                platform="vk",
                action="program_lesson",
                context={"program_id": program_id, "content_kind": "text"},
                surface="official",
            )

    def test_beginner_program_delivery_uses_existing_canonical_delivery_result(self) -> None:
        actor = _actor(PlatformRole.MANAGER)
        program_id = str(uuid4())
        customer_id = str(uuid4())
        active = SimpleNamespace(id=program_id, status=SimpleNamespace(value="active"))
        customer = SimpleNamespace(id=customer_id, display_name="Анна")
        prepared = SimpleNamespace(program=SimpleNamespace(program=SimpleNamespace(title="Курс")))
        with (
            patch.object(ui, "list_programs", return_value=[active]),
            patch.object(ui, "list_customers_with_active_identity", return_value=[customer]),
            patch.object(ui, "prepare_native_program_delivery", return_value=prepared) as deliver,
        ):
            message = ui._render(
                actor,
                ui.ParsedMemberInteraction("program-deliver-to", (program_id, customer_id)),
                linked=False,
                setup_issuer=None,
                setup_key="event-delivery-button",
                current_platform=ui.ConnectionPlatform.VK,
            )
        deliver.assert_called_once_with(
            actor=actor,
            program_id=program_id,
            customer_id=customer_id,
            platform=ui.ConnectionPlatform.VK,
        )
        self.assertIn("поставлена в очередь", message.text)

    def test_beginner_selection_routes_are_human_readable_and_keep_legacy_commands(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        publication = ui._publication_new_help(actor)
        self.assertIn("выберите", publication.text.casefold())
        self.assertIn("cpm:publication-new-for:vk", _commands(publication))
        self.assertNotIn("черновик telegram", publication.text.casefold())

        program_id = str(uuid4())
        with patch.object(
            ui,
            "get_program_draft",
            return_value=SimpleNamespace(program=SimpleNamespace(id=program_id, title="Курс")),
        ):
            lesson = ui._program_lesson_help(actor, program_id)
        self.assertIn("что вы хотите добавить", lesson.text.casefold())
        self.assertIn(f"cpm:program-lesson-kind:{program_id}:text", _commands(lesson))

        # The advanced syntax is deliberately still parsed after the simpler UI was added.
        self.assertEqual(
            ui.parse_native_member_interaction(
                f"урок {program_id[:8]} text | Введение | Старый расширенный путь"
            ).action,
            "program-lesson-text",
        )

    def test_forged_native_write_actions_fail_closed_for_read_only_role(self) -> None:
        actor = _actor(PlatformRole.ANALYST)
        for parsed in (
            ui.ParsedMemberInteraction("publication-new"),
            ui.ParsedMemberInteraction("payment-new"),
            ui.ParsedMemberInteraction("autopilot-enable"),
            ui.ParsedMemberInteraction("autopilot-disable"),
            ui.ParsedMemberInteraction("member-add-help"),
            ui.ParsedMemberInteraction("activity-edit-help"),
            ui.ParsedMemberInteraction("program-create"),
            ui.ParsedMemberInteraction("offering-new"),
        ):
            with self.subTest(action=parsed.action):
                message = ui._render(
                    actor,
                    parsed,
                    linked=False,
                    setup_issuer=None,
                    setup_key="event",
                )
                self.assertIn("недоступен", message.text.casefold())


if __name__ == "__main__":
    unittest.main()

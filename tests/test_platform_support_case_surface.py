from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import clientplatform_entry as entry
from clientplatform.application import support_cases
from clientplatform.domain.support_cases import SupportCaseCategory, SupportCaseStatus
from clientplatform.infrastructure.support_case_repository import SupportCaseConflict, SupportCaseUnavailable


class PlatformSupportCaseSurfaceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def message(text: str, *, user_id: int = 9001, message_id: int = 10):
        return SimpleNamespace(
            text=text,
            message_id=message_id,
            chat=SimpleNamespace(id=77),
            from_user=SimpleNamespace(id=user_id, username="operator", full_name="Operator"),
            answer=AsyncMock(),
        )

    async def test_queue_denies_non_operator_without_case_data(self) -> None:
        message = self.message("/supportqueue list", user_id=17)
        with (
            patch.object(entry.control, "_user_id", return_value=17),
            patch.object(
                support_cases,
                "list_platform_support_queue",
                side_effect=support_cases.PlatformSupportCasePermissionDenied(
                    "platform support queue access required"
                ),
            ) as queue,
        ):
            await entry.clientplatform_platform_support_queue_command(message)
        queue.assert_called_once_with(17, limit=50)
        message.answer.assert_awaited_once_with("Доступ к support queue недоступен.")

    async def test_queue_claim_delegates_to_application_boundary(self) -> None:
        case_id = "f3b3c9dd-fcb1-43ad-b911-32dfd81222ac"
        case = SimpleNamespace(
            id=case_id,
            business_id="ad67e150-0d91-48c9-a879-44a44782250d",
            category=SupportCaseCategory.TECHNICAL,
            status=SupportCaseStatus.CLAIMED,
            summary="Messenger unavailable",
        )
        message = self.message(f"/supportqueue claim {case_id}", message_id=11)
        with (
            patch.object(entry.control, "_user_id", return_value=9001),
            patch.object(support_cases, "claim_platform_support_case", return_value=case) as claim,
        ):
            await entry.clientplatform_platform_support_queue_command(message)
        claim.assert_called_once_with(
            9001,
            case_id=case_id,
            idempotency_key="telegram:77:11",
        )
        text = message.answer.await_args.args[0]
        self.assertIn(case_id, text)
        self.assertIn("claimed", text)

    async def test_tenant_support_create_uses_resolved_tenant_actor(self) -> None:
        business_id = "ad67e150-0d91-48c9-a879-44a44782250d"
        actor = SimpleNamespace(business_id=business_id)
        access = SimpleNamespace(business=SimpleNamespace(id=business_id, name="Acme"))
        case = SimpleNamespace(
            id="f3b3c9dd-fcb1-43ad-b911-32dfd81222ac",
            business_id=business_id,
            category=SupportCaseCategory.SECURITY,
            status=SupportCaseStatus.OPEN,
        )
        message = self.message("/support security Suspicious login", user_id=101, message_id=12)
        with (
            patch.object(entry.control, "_user_id", return_value=101),
            patch.object(entry, "list_accessible_businesses", return_value=[access]),
            patch.object(entry, "resolve_tenant_context", return_value=actor),
            patch.object(support_cases, "create_support_case", return_value=case) as create,
        ):
            await entry.clientplatform_support_case_command(message)
        create.assert_called_once_with(
            actor=actor,
            category="security",
            summary="Suspicious login",
            idempotency_key="telegram:77:12",
        )
        self.assertIn(case.id, message.answer.await_args.args[0])


    @staticmethod
    def case(*, status=SupportCaseStatus.OPEN, summary="Need help"):
        return SimpleNamespace(
            id="f3b3c9dd-fcb1-43ad-b911-32dfd81222ac",
            business_id="ad67e150-0d91-48c9-a879-44a44782250d",
            category=SupportCaseCategory.TECHNICAL,
            status=status,
            summary=summary,
        )

    def test_telegram_support_actor_handles_empty_multi_and_stale_context(self) -> None:
        first = SimpleNamespace(business=SimpleNamespace(id="b1"))
        second = SimpleNamespace(business=SimpleNamespace(id="b2"))
        with patch.object(entry, "list_accessible_businesses", return_value=[]):
            actor, accesses = entry._telegram_support_actor(101)
        self.assertIsNone(actor)
        self.assertEqual(accesses, [])

        with (
            patch.object(entry, "list_accessible_businesses", return_value=[first, second]),
            patch.object(entry, "get_owner_control_workspace", return_value=None),
        ):
            actor, accesses = entry._telegram_support_actor(101)
        self.assertIsNone(actor)
        self.assertEqual(len(accesses), 2)

        with (
            patch.object(entry, "list_accessible_businesses", return_value=[first, second]),
            patch.object(entry, "get_owner_control_workspace", return_value="b2"),
            patch.object(entry, "resolve_tenant_context", side_effect=RuntimeError("stale")),
        ):
            actor, accesses = entry._telegram_support_actor(101)
        self.assertIsNone(actor)
        self.assertEqual(len(accesses), 2)

    async def test_tenant_support_handles_missing_contexts_and_usage(self) -> None:
        no_business = self.message("/support list", user_id=101)
        multi = self.message("/support list", user_id=101)
        usage = self.message("/support", user_id=101)
        actor = SimpleNamespace(business_id="b1")
        with patch.object(entry.control, "_user_id", return_value=101):
            with patch.object(entry, "_telegram_support_actor", return_value=(None, [])):
                await entry.clientplatform_support_case_command(no_business)
            with patch.object(entry, "_telegram_support_actor", return_value=(None, [object(), object()])):
                await entry.clientplatform_support_case_command(multi)
            with patch.object(entry, "_telegram_support_actor", return_value=(actor, [object()])):
                await entry.clientplatform_support_case_command(usage)
        self.assertIn("Сначала подключите бизнес", no_business.answer.await_args.args[0])
        self.assertIn("несколько бизнесов", multi.answer.await_args.args[0])
        self.assertIn("Формат", usage.answer.await_args.args[0])

    async def test_tenant_support_list_empty_and_populated(self) -> None:
        actor = SimpleNamespace(business_id="b1")
        empty = self.message("/support list", user_id=101)
        populated = self.message("/support list", user_id=101)
        case = self.case(summary="Callback is unavailable")
        with (
            patch.object(entry.control, "_user_id", return_value=101),
            patch.object(entry, "_telegram_support_actor", return_value=(actor, [object()])),
            patch.object(support_cases, "list_tenant_support_cases", side_effect=[[], [case]]) as listing,
        ):
            await entry.clientplatform_support_case_command(empty)
            await entry.clientplatform_support_case_command(populated)
        self.assertEqual(listing.call_count, 2)
        self.assertIn("пока нет обращений", empty.answer.await_args.args[0])
        self.assertIn(case.id, populated.answer.await_args.args[0])
        self.assertIn(case.summary, populated.answer.await_args.args[0])

    async def test_tenant_support_validation_error_is_safe(self) -> None:
        actor = SimpleNamespace(business_id="b1")
        message = self.message("/support technical secret-looking-input", user_id=101)
        with (
            patch.object(entry.control, "_user_id", return_value=101),
            patch.object(entry, "_telegram_support_actor", return_value=(actor, [object()])),
            patch.object(support_cases, "create_support_case", side_effect=ValueError("invalid")),
        ):
            await entry.clientplatform_support_case_command(message)
        self.assertIn("Не удалось создать обращение", message.answer.await_args.args[0])

    async def test_queue_usage_empty_and_populated_list(self) -> None:
        usage = self.message("/supportqueue")
        empty = self.message("/supportqueue list", message_id=21)
        populated = self.message("/supportqueue list", message_id=22)
        case = self.case(summary="Provider callback failed")
        with patch.object(entry.control, "_user_id", return_value=9001):
            await entry.clientplatform_platform_support_queue_command(usage)
            with patch.object(support_cases, "list_platform_support_queue", side_effect=[[], [case]]) as listing:
                await entry.clientplatform_platform_support_queue_command(empty)
                await entry.clientplatform_platform_support_queue_command(populated)
        self.assertIn("Использование", usage.answer.await_args.args[0])
        self.assertIn("нет", empty.answer.await_args.args[0])
        self.assertEqual(listing.call_count, 2)
        text = populated.answer.await_args.args[0]
        self.assertIn(case.id, text)
        self.assertIn(case.business_id, text)
        self.assertIn(case.summary, text)

    async def test_queue_release_and_resolve_delegate_with_exact_operation_key(self) -> None:
        case = self.case(status=SupportCaseStatus.CLAIMED)
        for action, function_name in (
            ("release", "release_platform_support_case"),
            ("resolve", "resolve_platform_support_case"),
        ):
            message = self.message(f"/supportqueue {action} {case.id}", message_id=30 if action == "release" else 31)
            with (
                patch.object(entry.control, "_user_id", return_value=9001),
                patch.object(support_cases, function_name, return_value=case) as operation,
            ):
                await entry.clientplatform_platform_support_queue_command(message)
            operation.assert_called_once()
            kwargs = operation.call_args.kwargs
            self.assertEqual(kwargs["case_id"], case.id)
            self.assertTrue(str(kwargs["idempotency_key"]).startswith("telegram:77:"))
            self.assertIn(case.id, message.answer.await_args.args[0])

    async def test_queue_session_usage_success_and_unknown_action(self) -> None:
        case = self.case(status=SupportCaseStatus.CLAIMED)
        bad = self.message(f"/supportqueue session {case.id}", message_id=40)
        good = self.message(f"/supportqueue session {case.id} inspect exact case", message_id=41)
        unknown = self.message(f"/supportqueue nope {case.id}", message_id=42)
        session = SimpleNamespace(id="session-1", business_id=case.business_id, expires_at="2026-09-02T18:00:00+00:00")
        with patch.object(entry.control, "_user_id", return_value=9001):
            await entry.clientplatform_platform_support_queue_command(bad)
            with patch.object(support_cases, "issue_support_session_for_case", return_value=session) as issue:
                await entry.clientplatform_platform_support_queue_command(good)
            await entry.clientplatform_platform_support_queue_command(unknown)
        self.assertIn("Использование", bad.answer.await_args.args[0])
        issue.assert_called_once_with(
            9001,
            case_id=case.id,
            reason="inspect exact case",
            idempotency_key="telegram:77:41",
        )
        self.assertIn("session-1", good.answer.await_args.args[0])
        self.assertIn("Использование", unknown.answer.await_args.args[0])

    async def test_queue_missing_case_id_and_error_mapping(self) -> None:
        missing = self.message("/supportqueue claim", message_id=50)
        unavailable = self.message(f"/supportqueue claim {self.case().id}", message_id=51)
        conflict = self.message(f"/supportqueue claim {self.case().id}", message_id=52)
        denied = self.message(f"/supportqueue session {self.case().id} reason", message_id=53)
        with patch.object(entry.control, "_user_id", return_value=9001):
            await entry.clientplatform_platform_support_queue_command(missing)
            with patch.object(support_cases, "claim_platform_support_case", side_effect=SupportCaseUnavailable("gone")):
                await entry.clientplatform_platform_support_queue_command(unavailable)
            with patch.object(support_cases, "claim_platform_support_case", side_effect=SupportCaseConflict("race")):
                await entry.clientplatform_platform_support_queue_command(conflict)
            with patch.object(support_cases, "issue_support_session_for_case", side_effect=PermissionError("not claimed")):
                await entry.clientplatform_platform_support_queue_command(denied)
        self.assertIn("Использование", missing.answer.await_args.args[0])
        self.assertIn("состояние уже изменилось", unavailable.answer.await_args.args[0])
        self.assertIn("другим оператором", conflict.answer.await_args.args[0])
        self.assertIn("недоступна", denied.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()

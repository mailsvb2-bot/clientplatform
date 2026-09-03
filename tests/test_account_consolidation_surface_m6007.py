from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import clientplatform_entry as entry
from services.accounts import consolidation


class AccountConsolidationSurfaceM6007Tests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def message(text: str, *, user_id: int = 9001):
        return SimpleNamespace(
            text=text,
            from_user=SimpleNamespace(id=user_id, username="operator", full_name="Operator"),
            answer=AsyncMock(),
        )

    @staticmethod
    def plan(*, can_apply: bool = True, blockers=()):
        return SimpleNamespace(
            can_apply=can_apply,
            source_account_id=20002,
            target_account_id=10001,
            source_user_id=20002,
            target_user_id=10001,
            source_platforms=("vk",),
            target_platforms=("telegram",),
            plan_fingerprint="a" * 64,
            confirmation_code="MERGE-20002-TO-10001-aaaaaaaaaaaa",
            access_expansions=(
                SimpleNamespace(business_id="business-1", role="owner", status="active"),
            ),
            blockers=tuple(blockers),
            dependencies=(
                SimpleNamespace(
                    table="business_members",
                    column="user_id",
                    policy="repoint_authorization",
                    source_rows=1,
                    target_rows=0,
                ),
            ),
        )

    async def test_plan_maps_exact_ids_and_renders_safe_contract(self) -> None:
        message = self.message("/accountmerge plan 20002 10001")
        with (
            patch.object(entry.control, "_user_id", return_value=9001),
            patch.object(
                consolidation,
                "plan_account_consolidation",
                return_value=self.plan(),
            ) as plan,
        ):
            await entry.clientplatform_account_merge_command(message)
        plan.assert_called_once_with(
            9001, source_account_id=20002, target_account_id=10001
        )
        text = "\n".join(call.args[0] for call in message.answer.await_args_list)
        self.assertIn("READY", text)
        self.assertIn("Plan SHA-256", text)
        self.assertIn("Confirmation:", text)
        self.assertIn("Tenant access expansion", text)
        self.assertIn("business_members.user_id", text)
        self.assertNotIn("external_user_id", text)

    async def test_blocked_plan_has_no_apply_confirmation(self) -> None:
        message = self.message("/accountmerge plan 20002 10001")
        with (
            patch.object(entry.control, "_user_id", return_value=9001),
            patch.object(
                consolidation,
                "plan_account_consolidation",
                return_value=self.plan(
                    can_apply=False,
                    blockers=("membership_overlap:business-1",),
                ),
            ),
        ):
            await entry.clientplatform_account_merge_command(message)
        text = "\n".join(call.args[0] for call in message.answer.await_args_list)
        self.assertIn("BLOCKED", text)
        self.assertIn("membership_overlap:business-1", text)
        self.assertNotIn("Confirmation:", text)

    async def test_apply_requires_all_review_artifacts_and_preserves_reason(self) -> None:
        fingerprint = "b" * 64
        confirmation = "MERGE-20002-TO-10001-bbbbbbbbbbbb"
        message = self.message(
            f"/accountmerge apply 20002 10001 {fingerprint} {confirmation} op-7 Verified duplicate account"
        )
        result = consolidation.AccountConsolidationResult(
            operation_id="operation-7",
            source_account_id=20002,
            target_account_id=10001,
            source_user_id=20002,
            target_user_id=10001,
            plan_fingerprint=fingerprint,
            applied_at="2026-09-03T14:00:00+00:00",
            idempotent_replay=False,
        )
        with (
            patch.object(entry.control, "_user_id", return_value=9001),
            patch.object(
                consolidation,
                "apply_account_consolidation",
                return_value=result,
            ) as apply,
        ):
            await entry.clientplatform_account_merge_command(message)
        apply.assert_called_once_with(
            9001,
            source_account_id=20002,
            target_account_id=10001,
            expected_plan_fingerprint=fingerprint,
            confirmation_code=confirmation,
            idempotency_key="op-7",
            reason="Verified duplicate account",
        )
        self.assertIn("operation-7", message.answer.await_args.args[0])

    async def test_permission_and_stale_plan_map_to_safe_operator_messages(self) -> None:
        for error, expected in (
            (consolidation.AccountConsolidationPermissionDenied("denied"), "недоступен"),
            (consolidation.AccountConsolidationStalePlan("stale"), "устарел"),
        ):
            message = self.message("/accountmerge plan 20002 10001")
            with (
                patch.object(entry.control, "_user_id", return_value=17),
                patch.object(
                    consolidation,
                    "plan_account_consolidation",
                    side_effect=error,
                ),
            ):
                await entry.clientplatform_account_merge_command(message)
            self.assertIn(expected, message.answer.await_args.args[0])

    async def test_invalid_syntax_does_not_enter_service(self) -> None:
        for text in ("/accountmerge", "/accountmerge plan 20002", "/accountmerge nope 1 2"):
            message = self.message(text)
            with patch.object(
                consolidation,
                "plan_account_consolidation",
                side_effect=AssertionError("must not call service"),
            ):
                await entry.clientplatform_account_merge_command(message)
            self.assertIn("Использование", message.answer.await_args.args[0])

    def test_plan_chunks_stay_within_telegram_limit(self) -> None:
        dependencies = tuple(
            SimpleNamespace(
                table=f"table_{index:03d}",
                column="future_user_id",
                policy="retain_history",
                source_rows=1,
                target_rows=0,
            )
            for index in range(160)
        )
        plan = self.plan()
        plan.dependencies = dependencies
        chunks = entry._account_merge_plan_chunks(plan)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= entry._TELEGRAM_SAFE_TEXT_LIMIT for chunk in chunks))

    async def test_accountmerge_stays_out_of_public_command_menu(self) -> None:
        bot = SimpleNamespace(set_my_commands=AsyncMock(return_value=True))
        self.assertTrue(await entry.register_clientplatform_bot_commands(bot))
        commands = bot.set_my_commands.await_args.args[0]
        self.assertNotIn("accountmerge", {item.command for item in commands})


if __name__ == "__main__":
    unittest.main()

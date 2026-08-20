from __future__ import annotations

import importlib
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from clientplatform.application.retention import RetentionCandidateUnavailable
from clientplatform.domain.retention import ReactivationAction, RetentionCandidate, RetentionCohort

sales = importlib.import_module("handlers.clientplatform_sales")
control = importlib.import_module("handlers.clientplatform_control")


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self, user_id: int = 101) -> None:
        self.from_user = FakeUser(user_id)
        self.answers: list[tuple[str, dict[str, object]]] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        self.answers.append((text, kwargs))


class FakeCallback:
    def __init__(self, data: str, user_id: int = 101) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(user_id)
        self.answers: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def answer(self, *args: object, **kwargs: object) -> None:
        self.answers.append((args, kwargs))


class FakeState:
    def __init__(self) -> None:
        self.clear_count = 0

    async def clear(self) -> None:
        self.clear_count += 1


async def direct_to_thread(func, *args, **kwargs):
    return func(*args, **kwargs)


class ClientPlatformRetentionOwnerUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.business_id = str(uuid4())
        self.customer_id = str(uuid4())
        self.actor = SimpleNamespace(user_id=101, business_id=self.business_id)
        self.patches = [
            patch.object(sales.asyncio, "to_thread", direct_to_thread),
            patch.object(control, "Message", FakeMessage),
            patch.object(control, "_actor", AsyncMock(return_value=self.actor)),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def _candidate(self) -> RetentionCandidate:
        stamp = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        return RetentionCandidate(
            customer_id=self.customer_id,
            display_name="Анна",
            cohort=RetentionCohort.INACTIVE_CUSTOMER,
            suggested_action=ReactivationAction.REVIEW_REACTIVATION_OFFER,
            paid_orders=2,
            last_paid_at=stamp,
            last_activity_at=stamp,
            inactive_days=111,
        )

    async def test_retention_list_is_plain_language_and_callbacks_fit_telegram(self) -> None:
        with patch.object(sales, "list_retention_candidates", return_value=[self._candidate()]):
            callback = FakeCallback(f"cps:sr:{control._uuid_token(self.business_id)}")
            state = FakeState()
            await sales.open_retention_candidates(callback, state)

        text, kwargs = callback.message.answers[-1]
        self.assertIn("♻️ Вернуть клиентов", text)
        self.assertIn("Давно не возвращался", text)
        self.assertIn("ничего не отправляет сама", text)
        buttons = [
            button
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        approval = next(button for button in buttons if button.text == "✅ Взять в работу 1")
        self.assertTrue(str(approval.callback_data).endswith(":i"))
        self.assertLessEqual(len(str(approval.callback_data).encode("utf-8")), 64)
        self.assertEqual(state.clear_count, 1)

    async def test_owner_approval_creates_work_but_explicitly_sends_nothing(self) -> None:
        captured: dict[str, object] = {}

        def prepare(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(route_platform="telegram")

        with patch.object(sales, "prepare_reactivation_sales_lead", side_effect=prepare):
            callback = FakeCallback(
                f"cps:srr:{control._uuid_token(self.business_id)}:"
                f"{control._uuid_token(self.customer_id)}:i"
            )
            state = FakeState()
            await sales.approve_retention_candidate(callback, state)

        self.assertEqual(captured["customer_id"], self.customer_id)
        self.assertEqual(captured["expected_cohort"], RetentionCohort.INACTIVE_CUSTOMER)
        text, kwargs = callback.message.answers[-1]
        self.assertIn("Клиенту сейчас ничего не отправлено", text)
        self.assertIn("отдельного подтверждения", text)
        labels = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertIn("🛠 Открыть обращения", labels)
        self.assertEqual(state.clear_count, 1)

    async def test_stale_approval_fails_closed_without_success_message(self) -> None:
        with patch.object(
            sales,
            "prepare_reactivation_sales_lead",
            side_effect=RetentionCandidateUnavailable("changed"),
        ):
            callback = FakeCallback(
                f"cps:srr:{control._uuid_token(self.business_id)}:"
                f"{control._uuid_token(self.customer_id)}:i"
            )
            state = FakeState()
            await sales.approve_retention_candidate(callback, state)

        self.assertEqual(callback.message.answers, [])
        self.assertEqual(state.clear_count, 0)
        self.assertEqual(callback.answers[-1][0], ("Список изменился — обновите его перед действием.",))
        self.assertTrue(callback.answers[-1][1]["show_alert"])

    async def test_no_safe_route_is_presented_as_manual_work(self) -> None:
        with patch.object(
            sales,
            "prepare_reactivation_sales_lead",
            return_value=SimpleNamespace(route_platform=None),
        ):
            callback = FakeCallback(
                f"cps:srr:{control._uuid_token(self.business_id)}:"
                f"{control._uuid_token(self.customer_id)}:o"
            )
            await sales.approve_retention_candidate(callback, FakeState())

        text = callback.message.answers[-1][0]
        self.assertIn("ручная работа", text)
        self.assertIn("ничего не отправлено", text)

    async def test_malformed_callback_is_rejected_before_actor_resolution(self) -> None:
        callback = FakeCallback(f"cps:srr:{control._uuid_token(self.business_id)}:broken:x")
        await sales.approve_retention_candidate(callback, FakeState())
        self.assertEqual(callback.answers[-1][0], ("Список изменился — обновите его.",))
        control._actor.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

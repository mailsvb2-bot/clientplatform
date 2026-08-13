from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.application import sales_intelligence
from clientplatform.application.sales_intelligence import extract_customer_message_text


class SalesIntelligenceCaptureTests(unittest.TestCase):
    def test_extracts_message_text_and_caption(self) -> None:
        self.assertEqual(
            extract_customer_message_text({"message": {"text": "  Нужен   аудит  "}}),
            "Нужен аудит",
        )
        self.assertEqual(
            extract_customer_message_text({"message": {"caption": " Сколько стоит? "}}),
            "Сколько стоит?",
        )

    def test_does_not_send_commands_to_sales_ai(self) -> None:
        self.assertIsNone(extract_customer_message_text({"message": {"text": "/start"}}))
        self.assertIsNone(extract_customer_message_text({"callback_query": {"data": "x"}}))

    def test_bounds_customer_text_before_storage(self) -> None:
        text = extract_customer_message_text({"message": {"text": "я" * 13_000}})
        self.assertIsNotNone(text)
        assert text is not None
        self.assertEqual(len(text), 12_000)

    def _exercise(self, *, ai_allowed: bool) -> list[str]:
        operations: list[str] = []

        class FakeConn:
            pass

        @contextmanager
        def fake_get_db():
            yield FakeConn()

        class FakeSalesRepository:
            def __init__(self, _conn):
                pass

            def create_or_refresh_lead(self, **kwargs):
                del kwargs
                operations.append("lead")
                return SimpleNamespace(id="lead-id")

            def record_event(self, **kwargs):
                del kwargs
                operations.append("message")
                return True

        class FakeJobRepository:
            def __init__(self, _conn):
                pass

            def enqueue(self, **kwargs):
                del kwargs
                operations.append("enqueue")
                return None

        def fake_orchestrate(**kwargs):
            del kwargs
            operations.append("orchestrate")
            return None

        route = SimpleNamespace(business_id="business-id", managed_bot_id="bot-id")
        link = SimpleNamespace(business_id="business-id", customer_id="customer-id")
        with (
            patch.object(sales_intelligence, "get_db", fake_get_db),
            patch.object(
                sales_intelligence,
                "business_sales_ai_enabled_in_conn",
                return_value=ai_allowed,
            ),
            patch.object(sales_intelligence, "_owner_actor", return_value=SimpleNamespace()),
            patch.object(sales_intelligence, "SalesRepository", FakeSalesRepository),
            patch.object(sales_intelligence, "SalesAIJobRepository", FakeJobRepository),
            patch.object(
                sales_intelligence,
                "orchestrate_sales_signal_in_transaction",
                fake_orchestrate,
            ),
        ):
            sales_intelligence.record_managed_bot_customer_message(
                route=route,
                customer_link=link,
                telegram_user_id=7,
                provider_update_id="11",
                message_text="Нужна консультация",
                runtime_ai_enabled=True,
                runtime_ai_consent_target="deepseek:api.deepseek.com",
            )
        return operations

    def test_ai_freshness_advances_before_canonical_planning(self) -> None:
        self.assertEqual(
            self._exercise(ai_allowed=True),
            ["lead", "message", "enqueue", "orchestrate"],
        )

    def test_deterministic_sales_still_runs_without_ai_consent(self) -> None:
        self.assertEqual(
            self._exercise(ai_allowed=False),
            ["lead", "orchestrate"],
        )


if __name__ == "__main__":
    unittest.main()

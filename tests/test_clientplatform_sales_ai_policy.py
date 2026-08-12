from __future__ import annotations

import unittest

from clientplatform.domain.sales_ai_policy import prepare_sales_ai_text, validated_sales_ai_milestones
from clientplatform.domain.sales_intelligence import SalesAIAnalysis


class SalesAIPolicyTests(unittest.TestCase):
    def analysis(self, **overrides) -> SalesAIAnalysis:
        payload = {
            "intent": "service_interest",
            "need_summary": "Нужен аудит рекламы для магазина",
            "purchase_readiness": 0.82,
            "confidence": 0.95,
            "pricing_question": False,
            "pricing_exception": False,
            "need_is_specific": True,
            "purchase_intent_explicit": True,
            "explicit_human_request": False,
            "sensitive_context": False,
            "negative_sentiment": False,
            "recommended_offer_kind": "audit",
            "reply_goal": "present_option",
            "reason": "Клиент сформулировал задачу и хочет выбрать услугу",
        }
        payload.update(overrides)
        return SalesAIAnalysis.from_mapping(payload)

    def test_redacted_mode_removes_direct_identifiers(self) -> None:
        prepared = prepare_sales_ai_text(
            "Пишите мне test@example.com или +7 999 123-45-67, карта 4111111111111111",
            mode="redacted",
        )
        self.assertTrue(prepared.redacted)
        self.assertNotIn("test@example.com", prepared.text)
        self.assertNotIn("999 123", prepared.text)
        self.assertNotIn("4111111111111111", prepared.text)

    def test_no_cloud_fails_closed(self) -> None:
        with self.assertRaises(PermissionError):
            prepare_sales_ai_text("hello", mode="no_cloud")

    def test_semantic_milestones_require_multiple_high_confidence_signals(self) -> None:
        events = [item.value for item in validated_sales_ai_milestones(self.analysis())]
        self.assertEqual(events, ["need_captured", "qualification_passed"])
        self.assertEqual(validated_sales_ai_milestones(self.analysis(confidence=0.8)), ())
        self.assertEqual(validated_sales_ai_milestones(self.analysis(sensitive_context=True)), ())


if __name__ == "__main__":
    unittest.main()

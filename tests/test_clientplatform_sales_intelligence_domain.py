from __future__ import annotations

import math
import unittest

from clientplatform.domain.sales_intelligence import (
    SalesAIAnalysis,
    SalesAIDraft,
    sales_ai_analysis_json_schema,
)


class SalesIntelligenceDomainTests(unittest.TestCase):
    def payload(self) -> dict[str, object]:
        return {
            "intent": "pricing",
            "need_summary": "Хочет понять стоимость аудита рекламы",
            "purchase_readiness": 0.82,
            "confidence": 0.93,
            "pricing_question": True,
            "pricing_exception": False,
            "need_is_specific": True,
            "purchase_intent_explicit": True,
            "explicit_human_request": False,
            "sensitive_context": False,
            "negative_sentiment": False,
            "recommended_offer_kind": "audit",
            "reply_goal": "answer_question",
            "reason": "Клиент прямо спросил цену и описал задачу.",
        }

    def test_analysis_accepts_strict_payload(self) -> None:
        analysis = SalesAIAnalysis.from_mapping(self.payload())
        self.assertEqual(analysis.intent.value, "pricing")
        self.assertEqual(analysis.recommended_offer_kind.value, "audit")
        self.assertEqual(analysis.to_event_payload()["confidence"], 0.93)

    def test_analysis_rejects_unknown_or_missing_keys(self) -> None:
        payload = self.payload()
        payload["surprise"] = True
        with self.assertRaisesRegex(ValueError, "keys mismatch"):
            SalesAIAnalysis.from_mapping(payload)
        payload = self.payload()
        del payload["reason"]
        with self.assertRaisesRegex(ValueError, "keys mismatch"):
            SalesAIAnalysis.from_mapping(payload)

    def test_analysis_rejects_non_finite_scores_and_truthy_strings(self) -> None:
        for bad in (math.nan, math.inf, -math.inf, 1.01, -0.01):
            payload = self.payload()
            payload["confidence"] = bad
            with self.assertRaises(ValueError):
                SalesAIAnalysis.from_mapping(payload)
        payload = self.payload()
        payload["sensitive_context"] = "false"
        with self.assertRaisesRegex(ValueError, "sensitive_context"):
            SalesAIAnalysis.from_mapping(payload)

    def test_handoff_requires_a_concrete_safety_signal(self) -> None:
        payload = self.payload()
        payload["reply_goal"] = "handoff"
        with self.assertRaisesRegex(ValueError, "concrete handoff signal"):
            SalesAIAnalysis.from_mapping(payload)
        payload["explicit_human_request"] = True
        self.assertEqual(SalesAIAnalysis.from_mapping(payload).reply_goal.value, "handoff")

    def test_draft_is_bounded(self) -> None:
        draft = SalesAIDraft.from_mapping({"text": "  Здравствуйте!  ", "confidence": 0.9})
        self.assertEqual(draft.text, "Здравствуйте!")
        with self.assertRaises(ValueError):
            SalesAIDraft.from_mapping({"text": "x" * 2501, "confidence": 0.9})

    def test_schema_is_strict(self) -> None:
        schema = sales_ai_analysis_json_schema()
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(set(schema["required"]), set(schema["properties"]))


if __name__ == "__main__":
    unittest.main()

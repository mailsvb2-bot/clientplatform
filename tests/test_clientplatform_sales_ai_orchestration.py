from __future__ import annotations

import unittest

from clientplatform.application.sales_ai_orchestration import canonical_sales_ai_parameters
from clientplatform.domain.sales_intelligence import (
    SalesAIAnalysis,
    SalesAIIntent,
    SalesAIOfferKind,
    SalesAIReplyGoal,
)


def _analysis(**overrides) -> SalesAIAnalysis:
    payload = dict(
        intent=SalesAIIntent.SERVICE_INTEREST,
        need_summary="Клиент описал задачу и хочет понять следующий шаг.",
        purchase_readiness=0.8,
        confidence=0.95,
        pricing_question=False,
        pricing_exception=False,
        need_is_specific=True,
        purchase_intent_explicit=True,
        explicit_human_request=False,
        sensitive_context=False,
        negative_sentiment=False,
        recommended_offer_kind=SalesAIOfferKind.AUDIT,
        reply_goal=SalesAIReplyGoal.ASK_QUALIFICATION,
        reason="Есть явный интерес к услуге.",
    )
    payload.update(overrides)
    return SalesAIAnalysis(**payload)


class SalesAIOrchestrationTests(unittest.TestCase):
    def test_ai_maps_only_observations_not_action_kind(self) -> None:
        parameters = canonical_sales_ai_parameters(_analysis())
        self.assertNotIn("action_kind", parameters)
        self.assertNotIn("event", parameters)
        self.assertEqual(parameters["model_confidence"], 0.95)
        self.assertEqual(parameters["evidence_score"], 0.8)

    def test_question_and_issue_request_canonical_response_path(self) -> None:
        question = canonical_sales_ai_parameters(
            _analysis(reply_goal=SalesAIReplyGoal.ANSWER_QUESTION)
        )
        issue = canonical_sales_ai_parameters(
            _analysis(
                intent=SalesAIIntent.SUPPORT,
                reply_goal=SalesAIReplyGoal.RESOLVE_ISSUE,
            )
        )
        self.assertTrue(question["unanswered_inbound"])
        self.assertTrue(issue["unanswered_inbound"])

    def test_offer_suggestion_only_requests_canonical_response_path(self) -> None:
        parameters = canonical_sales_ai_parameters(
            _analysis(reply_goal=SalesAIReplyGoal.PRESENT_OPTION)
        )
        self.assertTrue(parameters["unanswered_inbound"])
        self.assertNotIn("recommended_offer_kind", parameters)
        self.assertNotIn("action_kind", parameters)

    def test_handoff_flags_are_preserved_for_canonical_evaluator(self) -> None:
        parameters = canonical_sales_ai_parameters(
            _analysis(
                explicit_human_request=True,
                reply_goal=SalesAIReplyGoal.HANDOFF,
            )
        )
        self.assertTrue(parameters["explicit_human_request"])

    def test_bare_high_confidence_handoff_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "concrete handoff signal"):
            _analysis(reply_goal=SalesAIReplyGoal.HANDOFF)

    def test_low_confidence_handoff_can_fail_closed(self) -> None:
        analysis = _analysis(
            confidence=0.4,
            reply_goal=SalesAIReplyGoal.HANDOFF,
        )
        parameters = canonical_sales_ai_parameters(analysis)
        self.assertEqual(parameters["model_confidence"], 0.4)



if __name__ == "__main__":
    unittest.main()

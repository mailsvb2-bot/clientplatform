from __future__ import annotations

import asyncio
import unittest

from clientplatform.infrastructure.sales_ai_provider import (
    DeepSeekChatCompletionsSalesAIProvider,
    OpenAICompatibleChatSalesAIProvider,
    OpenAIResponsesSalesAIProvider,
    SalesAIProviderError,
    build_sales_ai_provider,
)
from clientplatform.runtime.sales_ai_config import SalesAIRuntimeConfig
from clientplatform.domain.sales_intelligence import SalesAIAnalysis, SalesAIVerifiedOffer


class FakeCredentials:
    def __init__(self) -> None:
        self.references: list[str] = []

    def resolve(self, reference: str) -> str:
        self.references.append(reference)
        return "test-api-key-not-a-real-secret"


class FakeTransport:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def post_json(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def analysis_json() -> str:
    return (
        '{"intent":"pricing","need_summary":"Спрашивает цену","purchase_readiness":0.8,'
        '"confidence":0.95,"pricing_question":true,"pricing_exception":false,'
        '"need_is_specific":true,"purchase_intent_explicit":true,'
        '"explicit_human_request":false,"sensitive_context":false,'
        '"negative_sentiment":false,"recommended_offer_kind":"audit",'
        '"reply_goal":"answer_question","reason":"Прямой вопрос о цене"}'
    )


def completed_output(text: str) -> dict:
    return {
        "status": "completed",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]}
        ],
    }


def chat_output(text: str, *, finish_reason: str = "stop") -> dict:
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": text},
            }
        ]
    }


class SalesAIProviderTests(unittest.TestCase):
    def deepseek_config(self) -> SalesAIRuntimeConfig:
        return SalesAIRuntimeConfig.from_env({"CLIENTPLATFORM_SALES_AI_ENABLED": "1"})

    def openai_config(self) -> SalesAIRuntimeConfig:
        return SalesAIRuntimeConfig.from_env(
            {
                "CLIENTPLATFORM_SALES_AI_ENABLED": "1",
                "CLIENTPLATFORM_SALES_AI_PROVIDER": "openai",
                "CLIENTPLATFORM_SALES_AI_MODEL": "gpt-test-model",
            }
        )

    def test_factory_selects_deepseek_openai_and_compatible(self) -> None:
        self.assertIsInstance(
            build_sales_ai_provider(self.deepseek_config(), credential_provider=FakeCredentials()),
            DeepSeekChatCompletionsSalesAIProvider,
        )
        self.assertIsInstance(
            build_sales_ai_provider(self.openai_config(), credential_provider=FakeCredentials()),
            OpenAIResponsesSalesAIProvider,
        )
        compatible = SalesAIRuntimeConfig.from_env(
            {
                "CLIENTPLATFORM_SALES_AI_ENABLED": "1",
                "CLIENTPLATFORM_SALES_AI_PROVIDER": "openai_compatible",
                "CLIENTPLATFORM_SALES_AI_MODEL": "vendor-model",
                "CLIENTPLATFORM_SALES_AI_BASE_URL": "https://models.example/v1",
                "CLIENTPLATFORM_SALES_AI_ALLOW_CUSTOM_ENDPOINT": "1",
                "CLIENTPLATFORM_SALES_AI_ALLOWED_HOSTS": "models.example",
            }
        )
        self.assertIsInstance(
            build_sales_ai_provider(compatible, credential_provider=FakeCredentials()),
            OpenAICompatibleChatSalesAIProvider,
        )

    def test_deepseek_uses_stable_chat_json_and_disables_thinking(self) -> None:
        transport = FakeTransport(chat_output(analysis_json()))
        credentials = FakeCredentials()
        provider = DeepSeekChatCompletionsSalesAIProvider(
            self.deepseek_config(), credential_provider=credentials, transport=transport
        )
        analysis = asyncio.run(
            provider.analyze(
                customer_text="Сколько стоит аудит?",
                current_stage="contacted",
                source_kind="telegram",
            )
        )
        self.assertEqual(analysis.intent.value, "pricing")
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(call["payload"]["response_format"], {"type": "json_object"})
        self.assertEqual(call["payload"]["thinking"], {"type": "disabled"})
        self.assertIn("valid JSON", call["payload"]["messages"][0]["content"])
        self.assertNotIn("test-api-key", str(call["payload"]))
        self.assertEqual(
            credentials.references,
            ["secret://env/CLIENTPLATFORM_SECRET_SALES_AI_DEEPSEEK_API_KEY"],
        )

    def test_openai_responses_keeps_store_false_and_strict_schema(self) -> None:
        transport = FakeTransport(completed_output(analysis_json()))
        provider = OpenAIResponsesSalesAIProvider(
            self.openai_config(), credential_provider=FakeCredentials(), transport=transport
        )
        asyncio.run(
            provider.analyze(customer_text="hello", current_stage="new", source_kind="telegram")
        )
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://api.openai.com/v1/responses")
        self.assertIs(call["payload"]["store"], False)
        self.assertIs(call["payload"]["text"]["format"]["strict"], True)

    def test_draft_receives_only_verified_price_snapshot(self) -> None:
        transport = FakeTransport(chat_output('{"text":"Стоимость 15000 RUB","confidence":0.9}'))
        provider = DeepSeekChatCompletionsSalesAIProvider(
            self.deepseek_config(), credential_provider=FakeCredentials(), transport=transport
        )
        analysis = SalesAIAnalysis.from_mapping(__import__("json").loads(analysis_json()))
        asyncio.run(
            provider.draft_reply(
                customer_text="Сколько стоит?",
                analysis=analysis,
                approved_action="respond",
                verified_offer=SalesAIVerifiedOffer(
                    title="Аудит", offering_id="offer-1", amount_minor=1500000, currency="RUB"
                ),
            )
        )
        user_payload = __import__("json").loads(transport.calls[0]["payload"]["messages"][1]["content"])
        self.assertEqual(user_payload["verified_offer"]["amount_minor"], 1500000)
        self.assertEqual(user_payload["verified_offer"]["currency"], "RUB")
        self.assertEqual(user_payload["verified_offer"]["price_text"], "15000.00 RUB")

    def test_chat_adapter_rejects_empty_truncated_and_wrong_schema(self) -> None:
        for response in (
            chat_output(""),
            chat_output(analysis_json(), finish_reason="length"),
            chat_output('{"intent":"pricing"}'),
        ):
            provider = DeepSeekChatCompletionsSalesAIProvider(
                self.deepseek_config(), credential_provider=FakeCredentials(), transport=FakeTransport(response)
            )
            with self.assertRaises(SalesAIProviderError):
                asyncio.run(
                    provider.analyze(customer_text="hello", current_stage="new", source_kind="telegram")
                )


if __name__ == "__main__":
    unittest.main()

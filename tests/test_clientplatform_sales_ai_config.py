from __future__ import annotations

import unittest

from clientplatform.runtime.sales_ai_config import SalesAIRuntimeConfig


class SalesAIRuntimeConfigTests(unittest.TestCase):
    def test_disabled_defaults_to_deepseek_without_needing_secret(self) -> None:
        config = SalesAIRuntimeConfig.from_env({})
        self.assertFalse(config.enabled)
        self.assertEqual(config.provider, "deepseek")
        self.assertEqual(config.base_url, "https://api.deepseek.com")
        self.assertEqual(config.model, "deepseek-v4-flash")

    def test_enabled_deepseek_uses_official_endpoint(self) -> None:
        config = SalesAIRuntimeConfig.from_env({"CLIENTPLATFORM_SALES_AI_ENABLED": "1"})
        self.assertTrue(config.enabled)
        self.assertEqual(config.provider, "deepseek")
        self.assertEqual(config.consent_target, "deepseek:https://api.deepseek.com")
        with self.assertRaisesRegex(ValueError, "official"):
            SalesAIRuntimeConfig.from_env(
                {
                    "CLIENTPLATFORM_SALES_AI_ENABLED": "1",
                    "CLIENTPLATFORM_SALES_AI_BASE_URL": "https://evil.example",
                }
            )

    def test_openai_requires_explicit_model_and_official_endpoint(self) -> None:
        with self.assertRaisesRegex(ValueError, "MODEL"):
            SalesAIRuntimeConfig.from_env(
                {
                    "CLIENTPLATFORM_SALES_AI_ENABLED": "1",
                    "CLIENTPLATFORM_SALES_AI_PROVIDER": "openai",
                }
            )
        config = SalesAIRuntimeConfig.from_env(
            {
                "CLIENTPLATFORM_SALES_AI_ENABLED": "1",
                "CLIENTPLATFORM_SALES_AI_PROVIDER": "openai",
                "CLIENTPLATFORM_SALES_AI_MODEL": "gpt-example",
            }
        )
        self.assertEqual(config.base_url, "https://api.openai.com/v1")

    def test_custom_compatible_endpoint_is_double_opt_in(self) -> None:
        base = {
            "CLIENTPLATFORM_SALES_AI_ENABLED": "1",
            "CLIENTPLATFORM_SALES_AI_PROVIDER": "openai_compatible",
            "CLIENTPLATFORM_SALES_AI_MODEL": "vendor-model",
            "CLIENTPLATFORM_SALES_AI_BASE_URL": "https://models.example/v1",
        }
        with self.assertRaisesRegex(ValueError, "ALLOW_CUSTOM_ENDPOINT"):
            SalesAIRuntimeConfig.from_env(base)
        config = SalesAIRuntimeConfig.from_env(
            {**base, "CLIENTPLATFORM_SALES_AI_ALLOW_CUSTOM_ENDPOINT": "1", "CLIENTPLATFORM_SALES_AI_ALLOWED_HOSTS": "models.example"}
        )
        self.assertEqual(config.consent_target, "openai_compatible:https://models.example/v1")

    def test_custom_endpoint_requires_allowlist_and_public_host(self) -> None:
        base = {
            "CLIENTPLATFORM_SALES_AI_ENABLED": "1",
            "CLIENTPLATFORM_SALES_AI_PROVIDER": "openai_compatible",
            "CLIENTPLATFORM_SALES_AI_MODEL": "vendor-model",
            "CLIENTPLATFORM_SALES_AI_ALLOW_CUSTOM_ENDPOINT": "1",
        }
        with self.assertRaisesRegex(ValueError, "ALLOWED_HOSTS"):
            SalesAIRuntimeConfig.from_env({**base, "CLIENTPLATFORM_SALES_AI_BASE_URL": "https://models.example/v1"})
        with self.assertRaises(ValueError):
            SalesAIRuntimeConfig.from_env({**base, "CLIENTPLATFORM_SALES_AI_BASE_URL": "https://127.0.0.1/v1", "CLIENTPLATFORM_SALES_AI_ALLOWED_HOSTS": "127.0.0.1"})

    def test_rejects_discontinued_deepseek_alias(self) -> None:
        with self.assertRaisesRegex(ValueError, "discontinued"):
            SalesAIRuntimeConfig.from_env({"CLIENTPLATFORM_SALES_AI_ENABLED": "1", "CLIENTPLATFORM_SALES_AI_MODEL": "deepseek-chat"})

    def test_allows_explicit_future_deepseek_model_id(self) -> None:
        config = SalesAIRuntimeConfig.from_env({
            "CLIENTPLATFORM_SALES_AI_ENABLED": "1",
            "CLIENTPLATFORM_SALES_AI_PROVIDER": "deepseek",
            "CLIENTPLATFORM_SALES_AI_MODEL": "deepseek-v5-example",
        })
        self.assertEqual(config.model, "deepseek-v5-example")

    def test_worker_claim_batch_is_exactly_one(self) -> None:
        with self.assertRaises(ValueError):
            SalesAIRuntimeConfig.from_env({"CLIENTPLATFORM_SALES_AI_ENABLED": "1", "CLIENTPLATFORM_SALES_AI_BATCH_SIZE": "2"})

    def test_worker_lease_must_outlive_provider_timeout(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceed provider timeout"):
            SalesAIRuntimeConfig.from_env(
                {
                    "CLIENTPLATFORM_SALES_AI_ENABLED": "1",
                    "CLIENTPLATFORM_SALES_AI_TIMEOUT_SEC": "30",
                    "CLIENTPLATFORM_SALES_AI_LOCK_TTL_SEC": "40",
                }
            )


if __name__ == "__main__":
    unittest.main()

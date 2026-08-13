from __future__ import annotations

import unittest

from scripts.clientplatform_sales_ai_preflight import validate_sales_ai_environment


class SalesAIPreflightTests(unittest.TestCase):
    def test_disabled_needs_no_secret(self) -> None:
        self.assertEqual(validate_sales_ai_environment({}), [])

    def test_deepseek_requires_deepseek_secret_without_exposing_it(self) -> None:
        errors = validate_sales_ai_environment({"CLIENTPLATFORM_SALES_AI_ENABLED": "1"})
        self.assertEqual(len(errors), 1)
        self.assertIn("CLIENTPLATFORM_SECRET_SALES_AI_DEEPSEEK_API_KEY", errors[0])

    def test_valid_deepseek_configuration_passes(self) -> None:
        self.assertEqual(
            validate_sales_ai_environment(
                {
                    "CLIENTPLATFORM_SALES_AI_ENABLED": "1",
                    "CLIENTPLATFORM_SECRET_SALES_AI_DEEPSEEK_API_KEY": "x" * 32,
                }
            ),
            [],
        )

    def test_openai_uses_its_own_secret(self) -> None:
        env = {
            "CLIENTPLATFORM_SALES_AI_ENABLED": "1",
            "CLIENTPLATFORM_SALES_AI_PROVIDER": "openai",
            "CLIENTPLATFORM_SALES_AI_MODEL": "gpt-example",
        }
        errors = validate_sales_ai_environment(env)
        self.assertIn("CLIENTPLATFORM_SECRET_SALES_AI_OPENAI_API_KEY", errors[0])
        env["CLIENTPLATFORM_SECRET_SALES_AI_OPENAI_API_KEY"] = "x" * 32
        self.assertEqual(validate_sales_ai_environment(env), [])


if __name__ == "__main__":
    unittest.main()

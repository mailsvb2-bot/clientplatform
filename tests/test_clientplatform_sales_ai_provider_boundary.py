from __future__ import annotations

from clientplatform.runtime.sales_ai_config import SalesAIRuntimeConfig


def test_custom_provider_requires_explicit_endpoint_and_host_allowlist() -> None:
    env = {
        "CLIENTPLATFORM_SALES_AI_ENABLED": "1",
        "CLIENTPLATFORM_SALES_AI_PROVIDER": "openai_compatible",
        "CLIENTPLATFORM_SALES_AI_MODEL": "vendor-model",
        "CLIENTPLATFORM_SALES_AI_BASE_URL": "https://models.example/v1",
    }
    try:
        SalesAIRuntimeConfig.from_env(env)
    except ValueError:
        pass
    else:
        raise AssertionError("custom provider endpoint must fail closed without opt-in")

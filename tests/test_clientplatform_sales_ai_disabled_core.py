from __future__ import annotations

from clientplatform.runtime.sales_ai_config import SalesAIRuntimeConfig


def test_sales_ai_runtime_is_feature_flagged_off_without_configuration() -> None:
    config = SalesAIRuntimeConfig.from_env({})
    assert config.enabled is False

from __future__ import annotations

import inspect

from clientplatform.application.sales_ai_orchestration import canonical_sales_ai_parameters


def test_ai_observation_mapping_cannot_supply_action_or_transition() -> None:
    source = inspect.getsource(canonical_sales_ai_parameters)
    assert '"action_kind"' not in source
    assert '"event"' not in source
    assert "model_confidence" in source
    assert "explicit_human_request" in source


def test_sales_ai_provider_has_no_messenger_dependency() -> None:
    from clientplatform.infrastructure import sales_ai_provider

    source = inspect.getsource(sales_ai_provider)
    assert "Bot(" not in source
    assert "send_message" not in source
    assert "feed_update" not in source

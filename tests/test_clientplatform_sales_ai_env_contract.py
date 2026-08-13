from __future__ import annotations

from pathlib import Path


def test_production_example_keeps_sales_ai_disabled_by_default() -> None:
    text = Path("deploy/clientplatform/clientplatform.production.env.example").read_text(
        encoding="utf-8"
    )
    assert "CLIENTPLATFORM_SALES_AI_ENABLED=0" in text
    assert "CLIENTPLATFORM_SALES_AI_PROVIDER=deepseek" in text
    assert "CLIENTPLATFORM_SALES_AI_MODEL=deepseek-v4-flash" in text
    assert "CLIENTPLATFORM_SECRET_SALES_AI_DEEPSEEK_API_KEY=" in text


def test_sales_ai_secrets_are_dedicated_and_not_shared_with_generic_ai() -> None:
    text = Path("deploy/clientplatform/clientplatform.production.env.example").read_text(
        encoding="utf-8"
    )
    assert "secret://env/CLIENTPLATFORM_SECRET_SALES_AI_DEEPSEEK_API_KEY" in text
    assert "CLIENTPLATFORM_SALES_AI_API_KEY_REFERENCE" in text

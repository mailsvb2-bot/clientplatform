from __future__ import annotations

from clientplatform.runtime.sales_ai_config import sales_ai_secret_env_name


def test_sales_ai_secret_reference_is_dedicated_namespace_only() -> None:
    assert (
        sales_ai_secret_env_name(
            "secret://env/CLIENTPLATFORM_SECRET_SALES_AI_DEEPSEEK_API_KEY"
        )
        == "CLIENTPLATFORM_SECRET_SALES_AI_DEEPSEEK_API_KEY"
    )
    for bad in (
        "secret://env/BOT_TOKEN",
        "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY",
        "plain-secret",
    ):
        try:
            sales_ai_secret_env_name(bad)
        except ValueError:
            continue
        raise AssertionError(f"unexpectedly accepted non-dedicated secret ref: {bad}")

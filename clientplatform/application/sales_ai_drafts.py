from __future__ import annotations

from clientplatform.application.sales_ai_orchestration import sales_ai_draft_egress_permit
from clientplatform.application.sales_ai_settings import get_business_sales_ai_enabled
from clientplatform.domain.sales import SalesActionKind
from clientplatform.domain.sales_intelligence import SalesAIDraft
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.sales_ai_provider import build_sales_ai_provider
from clientplatform.runtime.sales_ai_config import SalesAIRuntimeConfig
from clientplatform.runtime.secrets import EnvironmentCredentialProvider


def sales_ai_runtime_available() -> bool:
    try:
        return SalesAIRuntimeConfig.from_env().enabled
    except ValueError:
        return False


def sales_ai_runtime_provider_label() -> str:
    try:
        config = SalesAIRuntimeConfig.from_env()
    except ValueError:
        return "AI-провайдер"
    return config.provider_label


def sales_ai_runtime_consent_target() -> str:
    try:
        config = SalesAIRuntimeConfig.from_env()
    except ValueError:
        return "не настроен"
    return config.consent_target


def sales_ai_enabled_for_business(*, actor: TenantContext) -> bool:
    if not sales_ai_runtime_available():
        return False
    return get_business_sales_ai_enabled(actor=actor)


async def draft_sales_reply(*, actor: TenantContext, lead_id: str) -> SalesAIDraft:
    """Generate an owner-review draft behind the same egress barrier as analysis."""
    config = SalesAIRuntimeConfig.from_env()
    if not config.enabled:
        raise ValueError("sales AI runtime is disabled")
    credentials = EnvironmentCredentialProvider()
    credentials.resolve(config.api_key_reference)
    provider = build_sales_ai_provider(config, credential_provider=credentials)
    async with sales_ai_draft_egress_permit(
        actor=actor,
        lead_id=lead_id,
        consent_target=config.consent_target,
    ) as (_lead, customer_text, analysis, action, verified_offer):
        if action in {SalesActionKind.HUMAN_HANDOFF.value, SalesActionKind.NOOP.value}:
            raise ValueError("this sales item requires human handling instead of an AI draft")
        return await provider.draft_reply(
            customer_text=customer_text,
            analysis=analysis,
            approved_action=action,
            verified_offer=verified_offer,
        )


__all__ = [
    "draft_sales_reply",
    "sales_ai_enabled_for_business",
    "sales_ai_runtime_available",
    "sales_ai_runtime_consent_target",
    "sales_ai_runtime_provider_label",
]

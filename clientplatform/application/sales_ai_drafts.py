from __future__ import annotations

import asyncio

from clientplatform.application.sales_ai_orchestration import (
    sales_ai_draft_egress_permit,
    validate_sales_ai_draft_freshness,
)
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
    """Generate a current owner-review draft behind the tenant egress barrier.

    The provider request is made only from the latest validated evidence. After the
    network call returns, the source-order head and canonical plan are checked again
    before the draft is exposed, so a customer reply arriving mid-generation makes
    the draft fail closed instead of showing stale guidance.
    """
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
    ) as evidence:
        if evidence.action_kind in {
            SalesActionKind.HUMAN_HANDOFF.value,
            SalesActionKind.NOOP.value,
        }:
            raise ValueError("this sales item requires human handling instead of an AI draft")
        draft = await provider.draft_reply(
            customer_text=evidence.customer_text,
            analysis=evidence.analysis,
            approved_action=evidence.action_kind,
            verified_offer=evidence.verified_offer,
        )
        expected_source_order_key = evidence.source_order_key
        expected_plan_id = evidence.plan_id

    await asyncio.to_thread(
        validate_sales_ai_draft_freshness,
        actor=actor,
        lead_id=lead_id,
        expected_source_order_key=expected_source_order_key,
        expected_plan_id=expected_plan_id,
    )
    return draft


__all__ = [
    "draft_sales_reply",
    "sales_ai_enabled_for_business",
    "sales_ai_runtime_available",
    "sales_ai_runtime_consent_target",
    "sales_ai_runtime_provider_label",
]

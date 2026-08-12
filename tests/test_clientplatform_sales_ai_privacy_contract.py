from __future__ import annotations

from clientplatform.privacy_manifest import TENANT_POLICIES


def test_sales_ai_customer_linked_tables_have_explicit_privacy_policy() -> None:
    assert TENANT_POLICIES["clientplatform_sales_ai_jobs"].disposition == "erase"
    assert TENANT_POLICIES["clientplatform_sales_ai_heads"].disposition == "erase"
    assert TENANT_POLICIES["clientplatform_sales_ai_analysis_projection"].disposition == "erase"


def test_sales_ai_consent_is_business_owned_retained_configuration() -> None:
    policy = TENANT_POLICIES["clientplatform_sales_ai_consents"]
    assert policy.disposition == "retain"
    assert "consent" in policy.reason.lower()

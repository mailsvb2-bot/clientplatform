from __future__ import annotations

from clientplatform.runtime.owner import _CLIENTPLATFORM_REQUIRED_TABLES


def test_omnichannel_runtime_readiness_requires_identity_ingress_and_provider_tables() -> None:
    assert {
        "accounts",
        "account_channel_identities",
        "messenger_ingress_routes",
        "customer_channel_link_tokens",
        "provider_dispatch_outbox",
    }.issubset(_CLIENTPLATFORM_REQUIRED_TABLES)

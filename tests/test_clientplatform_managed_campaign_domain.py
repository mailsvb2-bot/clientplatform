from clientplatform.domain.managed_ad_campaigns import managed_campaign_provisioning_key


def test_managed_campaign_key_is_deterministic() -> None:
    value = managed_campaign_provisioning_key(
        business_id="00000000-0000-4000-8000-000000000001",
        promotion_campaign_id="00000000-0000-4000-8000-000000000002",
        connection_id="00000000-0000-4000-8000-000000000003",
    )
    assert value == managed_campaign_provisioning_key(
        business_id="00000000-0000-4000-8000-000000000001",
        promotion_campaign_id="00000000-0000-4000-8000-000000000002",
        connection_id="00000000-0000-4000-8000-000000000003",
    )
    assert value.startswith("cpmc_")

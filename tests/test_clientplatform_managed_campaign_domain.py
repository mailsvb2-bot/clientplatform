from __future__ import annotations

import unittest
from uuid import uuid4

from clientplatform.domain.managed_ad_campaigns import (
    managed_campaign_name,
    managed_campaign_provisioning_key,
)


class ManagedCampaignDomainTests(unittest.TestCase):
    def test_key_is_deterministic_and_opaque(self) -> None:
        kwargs = {
            "business_id": str(uuid4()),
            "promotion_campaign_id": str(uuid4()),
            "connection_id": str(uuid4()),
        }
        value = managed_campaign_provisioning_key(**kwargs)
        self.assertEqual(value, managed_campaign_provisioning_key(**kwargs))
        self.assertTrue(value.startswith("cpmc_"))
        self.assertNotIn(kwargs["business_id"], value)
        self.assertEqual(managed_campaign_name(value), f"ClientPlatform · {value}")


if __name__ == "__main__":
    unittest.main()

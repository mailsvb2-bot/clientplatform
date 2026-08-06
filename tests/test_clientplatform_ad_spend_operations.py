from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from clientplatform.domain.ad_spend import AdSpendInvariantViolation
from clientplatform.domain.ad_spend_operations import (
    AdSpendOperation,
    AdSpendOperationStatus,
    AdSpendOperationType,
    ad_spend_operation_key,
)
from clientplatform.integrations.yandex_direct import (
    YandexDirectError,
    YandexOAuthConfig,
)
from clientplatform.integrations.yandex_direct_actions import YandexDirectAdActions


NOW = datetime(2026, 8, 5, 19, 0, tzinfo=timezone.utc)


class ScriptedActions(YandexDirectAdActions):
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        super().__init__(
            oauth=YandexOAuthConfig(
                client_id="id",
                client_secret="secret",
                redirect_uri="https://example.test/callback",
            )
        )
        self.responses = list(responses)
        self.methods: list[str] = []

    def _call(
        self,
        *,
        access_token: str,
        payload: Mapping[str, Any],
        client_login: str = "",
    ) -> Mapping[str, Any]:
        self.methods.append(str(payload.get("method")))
        return self.responses.pop(0)


class SpendOperationDomainTests(unittest.TestCase):
    def test_key_is_tenant_and_operation_scoped(self) -> None:
        business = str(uuid4())
        authorization = str(uuid4())
        launch = ad_spend_operation_key(
            business_id=business,
            authorization_id=authorization,
            operation_type="launch",
        )
        self.assertEqual(
            launch,
            ad_spend_operation_key(
                business_id=business,
                authorization_id=authorization,
                operation_type=AdSpendOperationType.LAUNCH,
            ),
        )
        self.assertNotEqual(
            launch,
            ad_spend_operation_key(
                business_id=business,
                authorization_id=authorization,
                operation_type="stop",
            ),
        )

    def test_processing_requires_a_lease(self) -> None:
        business = str(uuid4())
        authorization = str(uuid4())
        with self.assertRaisesRegex(AdSpendInvariantViolation, "requires a lease"):
            AdSpendOperation(
                id=str(uuid4()),
                business_id=business,
                authorization_id=authorization,
                operation_type=AdSpendOperationType.LAUNCH,
                status=AdSpendOperationStatus.PROCESSING,
                idempotency_key=ad_spend_operation_key(
                    business_id=business,
                    authorization_id=authorization,
                    operation_type="launch",
                ),
                attempts=1,
                available_at=NOW.isoformat(),
                created_at=NOW.isoformat(),
                updated_at=NOW.isoformat(),
            )

    def test_provider_launch_reconciles_submitted_ad_without_second_mutation(self) -> None:
        provider = ScriptedActions(
            [
                {
                    "Ads": [
                        {
                            "Id": 77,
                            "AdGroupId": 88,
                            "CampaignId": 99,
                            "State": "ON",
                            "Status": "MODERATION",
                            "Type": "TEXT_AD",
                        }
                    ]
                }
            ]
        )
        result = provider.moderate_ad(
            access_token="token",
            external_ad_id="77",
            expected_campaign_id="99",
            captured_at=NOW,
        )
        self.assertTrue(result.reconciled_without_mutation)
        self.assertEqual(provider.methods, ["get"])

    def test_provider_launch_mutates_only_exact_draft_and_rechecks(self) -> None:
        provider = ScriptedActions(
            [
                {
                    "Ads": [
                        {
                            "Id": 77,
                            "AdGroupId": 88,
                            "CampaignId": 99,
                            "State": "OFF",
                            "Status": "DRAFT",
                            "Type": "TEXT_AD",
                        }
                    ]
                },
                {"ModerateResults": [{"Id": 77}]},
                {
                    "Ads": [
                        {
                            "Id": 77,
                            "AdGroupId": 88,
                            "CampaignId": 99,
                            "State": "OFF",
                            "Status": "MODERATION",
                            "Type": "TEXT_AD",
                        }
                    ]
                },
            ]
        )
        result = provider.moderate_ad(
            access_token="token",
            external_ad_id="77",
            expected_campaign_id="99",
            captured_at=NOW,
        )
        self.assertFalse(result.reconciled_without_mutation)
        self.assertEqual(provider.methods, ["get", "moderate", "get"])

    def test_provider_rejects_cross_campaign_identity(self) -> None:
        provider = ScriptedActions(
            [
                {
                    "Ads": [
                        {
                            "Id": 77,
                            "AdGroupId": 88,
                            "CampaignId": 100,
                            "State": "OFF",
                            "Status": "DRAFT",
                            "Type": "TEXT_AD",
                        }
                    ]
                }
            ]
        )
        with self.assertRaisesRegex(
            YandexDirectError,
            "campaign_identity_mismatch",
        ):
            provider.moderate_ad(
                access_token="token",
                external_ad_id="77",
                expected_campaign_id="99",
                captured_at=NOW,
            )

    def test_stop_is_idempotent_for_already_suspended_ad(self) -> None:
        provider = ScriptedActions(
            [
                {
                    "Ads": [
                        {
                            "Id": 77,
                            "AdGroupId": 88,
                            "CampaignId": 99,
                            "State": "SUSPENDED",
                            "Status": "ACCEPTED",
                            "Type": "TEXT_AD",
                        }
                    ]
                }
            ]
        )
        result = provider.suspend_ad(
            access_token="token",
            external_ad_id="77",
            expected_campaign_id="99",
            captured_at=NOW,
        )
        self.assertTrue(result.reconciled_without_mutation)
        self.assertEqual(provider.methods, ["get"])

    def test_stop_treats_unlaunched_draft_as_already_safe(self) -> None:
        provider = ScriptedActions(
            [
                {
                    "Ads": [
                        {
                            "Id": 77,
                            "AdGroupId": 88,
                            "CampaignId": 99,
                            "State": "OFF",
                            "Status": "DRAFT",
                            "Type": "TEXT_AD",
                        }
                    ]
                }
            ]
        )
        result = provider.suspend_ad(
            access_token="token",
            external_ad_id="77",
            expected_campaign_id="99",
            captured_at=NOW,
        )
        self.assertTrue(result.reconciled_without_mutation)
        self.assertEqual(result.after.status, "DRAFT")
        self.assertEqual(provider.methods, ["get"])


if __name__ == "__main__":
    unittest.main()

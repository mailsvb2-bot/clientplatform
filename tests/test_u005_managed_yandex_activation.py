from __future__ import annotations

import inspect
import json
import unittest
from datetime import datetime, timezone

from clientplatform.application.ad_spend_operations import process_one_ad_spend_operation
from clientplatform.integrations.yandex_direct import YandexDirectError, YandexOAuthConfig
from clientplatform.integrations.yandex_direct_actions import YandexDirectAdActions
from clientplatform.integrations.yandex_direct_budget import ReadOnlyYandexDirectBudgetProvider


NOW = datetime(2026, 8, 18, 7, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, *, method, url, headers, body=None, timeout=20.0):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        return self.responses.pop(0)


def _oauth() -> YandexOAuthConfig:
    return YandexOAuthConfig(
        client_id="client-id",
        client_secret="client-secret",
        redirect_uri="https://app.clientplatform.ru/oauth/yandex-direct/callback",
    )


def _response(payload: dict[str, object]) -> tuple[int, dict[str, str], bytes]:
    return 200, {}, json.dumps(payload).encode("utf-8")


def _managed_campaign(*, weekly: int | None = None, network: str = "SERVING_OFF") -> dict[str, object]:
    search: dict[str, object] = {"BiddingStrategyType": "HIGHEST_POSITION"}
    if weekly is not None:
        search["HighestPosition"] = {"WeeklySpendLimit": weekly}
    return {
        "Id": 6001,
        "Type": "UNIFIED_CAMPAIGN",
        "State": "OFF",
        "Status": "DRAFT",
        "StatusPayment": "ALLOWED",
        "Currency": "RUB",
        "Funds": {
            "Mode": "CAMPAIGN_FUNDS",
            "CampaignFunds": {
                "Sum": 500_000_000,
                "Balance": 123_450_000,
                "SumAvailableForTransfer": 100_000_000,
            },
        },
        "UnifiedCampaign": {
            "BiddingStrategy": {
                "Search": search,
                "Network": {"BiddingStrategyType": network},
            }
        },
    }


class ManagedBudgetSnapshotTests(unittest.TestCase):
    def test_safe_managed_draft_is_launch_eligible_and_read_via_v501(self) -> None:
        transport = FakeTransport(
            [_response({"result": {"Campaigns": [_managed_campaign()]}})]
        )
        provider = ReadOnlyYandexDirectBudgetProvider(
            oauth=_oauth(), transport=transport
        )

        campaign = provider.campaign_budget_readout(
            access_token="token",
            external_campaign_id="6001",
            captured_at=NOW,
            client_login="owner-login",
        )

        self.assertTrue(campaign.launch_eligible)
        self.assertEqual(campaign.campaign_type, "UNIFIED_CAMPAIGN")
        self.assertEqual(campaign.state, "OFF")
        self.assertEqual(campaign.status, "DRAFT")
        self.assertEqual(campaign.search_strategy, "HIGHEST_POSITION")
        self.assertEqual(campaign.network_strategy, "SERVING_OFF")
        call = transport.calls[0]
        self.assertIn("/json/v501/campaigns", str(call["url"]))
        body = json.loads(call["body"])
        self.assertEqual(body["params"]["UnifiedCampaignFieldNames"], ["BiddingStrategy"])

    def test_managed_draft_with_serving_network_is_not_launch_eligible(self) -> None:
        transport = FakeTransport(
            [
                _response(
                    {
                        "result": {
                            "Campaigns": [
                                _managed_campaign(network="NETWORK_DEFAULT")
                            ]
                        }
                    }
                )
            ]
        )
        provider = ReadOnlyYandexDirectBudgetProvider(
            oauth=_oauth(), transport=transport
        )
        campaign = provider.campaign_budget_readout(
            access_token="token",
            external_campaign_id="6001",
            captured_at=NOW,
        )
        self.assertFalse(campaign.launch_eligible)


class ManagedActivationTests(unittest.TestCase):
    def test_authorized_limits_are_applied_then_freshly_read_back(self) -> None:
        weekly_micros = 70_000_000
        transport = FakeTransport(
            [
                _response({"result": {"Campaigns": [_managed_campaign()]}}),
                _response({"result": {"UpdateResults": [{"Id": 6001}]}}),
                _response(
                    {
                        "result": {
                            "Campaigns": [
                                _managed_campaign(weekly=weekly_micros)
                            ]
                        }
                    }
                ),
            ]
        )
        provider = YandexDirectAdActions(oauth=_oauth(), transport=transport)

        result = provider.configure_managed_launch_budget(
            access_token="token",
            external_campaign_id="6001",
            hard_cap_minor=9_000,
            daily_cap_minor=1_000,
            currency="RUB",
            client_login="owner-login",
        )

        self.assertFalse(result.reconciled_without_mutation)
        self.assertEqual(result.weekly_spend_limit_micros, weekly_micros)
        self.assertEqual(len(transport.calls), 3)
        self.assertTrue(
            all("/json/v501/campaigns" in str(call["url"]) for call in transport.calls)
        )
        update = json.loads(transport.calls[1]["body"])
        self.assertEqual(update["method"], "update")
        strategy = update["params"]["Campaigns"][0]["UnifiedCampaign"]["BiddingStrategy"]
        self.assertEqual(strategy["Search"]["BiddingStrategyType"], "HIGHEST_POSITION")
        self.assertEqual(
            strategy["Search"]["HighestPosition"]["WeeklySpendLimit"],
            weekly_micros,
        )
        self.assertEqual(strategy["Network"], {"BiddingStrategyType": "SERVING_OFF"})
        self.assertNotIn("resume", json.dumps(update).lower())

    def test_existing_exact_budget_is_idempotent(self) -> None:
        weekly_micros = 50_000_000
        transport = FakeTransport(
            [
                _response(
                    {
                        "result": {
                            "Campaigns": [
                                _managed_campaign(weekly=weekly_micros)
                            ]
                        }
                    }
                )
            ]
        )
        provider = YandexDirectAdActions(oauth=_oauth(), transport=transport)
        result = provider.configure_managed_launch_budget(
            access_token="token",
            external_campaign_id="6001",
            hard_cap_minor=5_000,
            daily_cap_minor=1_000,
            currency="RUB",
        )
        self.assertTrue(result.reconciled_without_mutation)
        self.assertEqual(result.weekly_spend_limit_micros, weekly_micros)
        self.assertEqual(len(transport.calls), 1)

    def test_out_of_band_budget_or_strategy_drift_fails_before_write(self) -> None:
        drifted = _managed_campaign(weekly=10_000_000)
        transport = FakeTransport(
            [_response({"result": {"Campaigns": [drifted]}})]
        )
        provider = YandexDirectAdActions(oauth=_oauth(), transport=transport)
        with self.assertRaisesRegex(YandexDirectError, "budget_drift"):
            provider.configure_managed_launch_budget(
                access_token="token",
                external_campaign_id="6001",
                hard_cap_minor=5_000,
                daily_cap_minor=1_000,
                currency="RUB",
            )
        self.assertEqual(len(transport.calls), 1)

    def test_moderation_uses_v501_and_is_reconciled_by_exact_id(self) -> None:
        draft = {
            "Id": 77,
            "AdGroupId": 88,
            "CampaignId": 6001,
            "State": "OFF",
            "Status": "DRAFT",
            "Type": "TEXT_AD",
        }
        moderation = dict(draft, Status="MODERATION")
        transport = FakeTransport(
            [
                _response({"result": {"Ads": [draft]}}),
                _response({"result": {"ModerateResults": [{"Id": 77}]}}),
                _response({"result": {"Ads": [moderation]}}),
            ]
        )
        provider = YandexDirectAdActions(oauth=_oauth(), transport=transport)
        result = provider.moderate_ad(
            access_token="token",
            external_ad_id="77",
            expected_campaign_id="6001",
            captured_at=NOW,
        )
        self.assertEqual(result.after.status, "MODERATION")
        self.assertEqual(len(transport.calls), 3)
        self.assertTrue(
            all("/json/v501/ads" in str(call["url"]) for call in transport.calls)
        )
        methods = [json.loads(call["body"])["method"] for call in transport.calls]
        self.assertEqual(methods, ["get", "moderate", "get"])
        self.assertNotIn("resume", methods)

    def test_launch_runner_rechecks_guard_after_budget_reconciliation(self) -> None:
        source = inspect.getsource(process_one_ad_spend_operation)
        configure = source.index("configure_managed_launch_budget")
        second_guard = source.index(
            "fresh server-side spend guard rejected launch after budget reconciliation"
        )
        moderate = source.index("moderate_ad")
        self.assertLess(configure, second_guard)
        self.assertLess(second_guard, moderate)
        self.assertNotIn("resume_ad", source)


if __name__ == "__main__":
    unittest.main()

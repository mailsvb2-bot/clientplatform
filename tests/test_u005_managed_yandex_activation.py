from __future__ import annotations

import inspect
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from clientplatform.application import ad_goal_autopilot as goal
from clientplatform.application.ad_spend_operations import process_one_ad_spend_operation
from clientplatform.domain.ad_spend import AdSpendInvariantViolation
from clientplatform.integrations.yandex_direct import YandexDirectError, YandexOAuthConfig
from clientplatform.integrations.yandex_direct_actions import YandexDirectAdActions
from clientplatform.integrations.yandex_direct_budget import (
    ReadOnlyYandexDirectBudgetProvider,
    managed_strategy_matches_authorization,
    managed_strategy_string,
)


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


def _managed_campaign(
    *,
    weekly: int | None = None,
    network: str = "SERVING_OFF",
    state: str = "OFF",
    status: str = "DRAFT",
    status_payment: str = "DISALLOWED",
) -> dict[str, object]:
    search: dict[str, object] = {"BiddingStrategyType": "HIGHEST_POSITION"}
    if weekly is not None:
        search["HighestPosition"] = {"WeeklySpendLimit": weekly}
    return {
        "Id": 6001,
        "Type": "UNIFIED_CAMPAIGN",
        "State": state,
        "Status": status,
        "StatusPayment": status_payment,
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


def _campaign_readout(item: dict[str, object]):
    transport = FakeTransport([_response({"result": {"Campaigns": [item]}})])
    provider = ReadOnlyYandexDirectBudgetProvider(oauth=_oauth(), transport=transport)
    campaign = provider.campaign_budget_readout(
        access_token="token",
        external_campaign_id="6001",
        captured_at=NOW,
        client_login="owner-login",
    )
    return campaign, transport


class ManagedBudgetSnapshotTests(unittest.TestCase):
    def test_safe_managed_draft_is_launch_eligible_even_before_payment_allowed(self) -> None:
        campaign, transport = _campaign_readout(_managed_campaign())

        self.assertTrue(campaign.launch_eligible)
        self.assertEqual(campaign.campaign_type, "UNIFIED_CAMPAIGN")
        self.assertEqual(campaign.state, "OFF")
        self.assertEqual(campaign.status, "DRAFT")
        self.assertEqual(campaign.status_payment, "DISALLOWED")
        self.assertEqual(campaign.search_strategy, "HIGHEST_POSITION")
        self.assertEqual(campaign.network_strategy, "SERVING_OFF")
        self.assertIsNone(campaign.weekly_spend_limit_micros)
        call = transport.calls[0]
        self.assertIn("/json/v501/campaigns", str(call["url"]))
        body = json.loads(call["body"])
        self.assertEqual(body["params"]["UnifiedCampaignFieldNames"], ["BiddingStrategy"])

    def test_managed_moderation_and_accepted_states_remain_guard_eligible(self) -> None:
        moderation, _ = _campaign_readout(
            _managed_campaign(status="MODERATION", status_payment="DISALLOWED")
        )
        accepted, _ = _campaign_readout(
            _managed_campaign(
                weekly=50_000_000,
                state="ON",
                status="ACCEPTED",
                status_payment="ALLOWED",
            )
        )
        rejected, _ = _campaign_readout(
            _managed_campaign(status="REJECTED", status_payment="DISALLOWED")
        )

        self.assertTrue(moderation.launch_eligible)
        self.assertTrue(accepted.launch_eligible)
        self.assertFalse(rejected.launch_eligible)

    def test_managed_draft_with_serving_network_is_not_launch_eligible(self) -> None:
        campaign, _ = _campaign_readout(
            _managed_campaign(network="NETWORK_DEFAULT")
        )
        self.assertFalse(campaign.launch_eligible)

    def test_runtime_strategy_allows_only_consent_state_or_exact_authorized_limit(self) -> None:
        consented = managed_strategy_string(weekly_spend_limit_micros=50_000_000)
        target = managed_strategy_string(weekly_spend_limit_micros=70_000_000)
        drifted = managed_strategy_string(weekly_spend_limit_micros=60_000_000)

        self.assertTrue(
            managed_strategy_matches_authorization(
                consented_strategy=consented,
                current_strategy=consented,
                hard_cap_minor=9_000,
                daily_cap_minor=1_000,
                require_applied_limit=False,
            )
        )
        self.assertTrue(
            managed_strategy_matches_authorization(
                consented_strategy=consented,
                current_strategy=target,
                hard_cap_minor=9_000,
                daily_cap_minor=1_000,
                require_applied_limit=True,
            )
        )
        self.assertFalse(
            managed_strategy_matches_authorization(
                consented_strategy=consented,
                current_strategy=drifted,
                hard_cap_minor=9_000,
                daily_cap_minor=1_000,
                require_applied_limit=False,
            )
        )
        self.assertFalse(
            managed_strategy_matches_authorization(
                consented_strategy=consented,
                current_strategy=consented,
                hard_cap_minor=9_000,
                daily_cap_minor=1_000,
                require_applied_limit=True,
            )
        )


class ProviderMinimumBudgetTests(unittest.TestCase):
    def test_dictionary_reads_exact_minimum_weekly_budget_via_v501(self) -> None:
        transport = FakeTransport(
            [
                _response(
                    {
                        "result": {
                            "Currencies": [
                                {
                                    "Currency": "RUB",
                                    "Properties": [
                                        {"Name": "MinimumBid", "Value": "300000"},
                                        {
                                            "Name": "MinimumWeeklySpendLimit",
                                            "Value": "300000000",
                                        },
                                    ],
                                },
                                {
                                    "Currency": "USD",
                                    "Properties": [
                                        {
                                            "Name": "MinimumWeeklySpendLimit",
                                            "Value": "5000000",
                                        }
                                    ],
                                },
                            ]
                        }
                    }
                )
            ]
        )
        provider = ReadOnlyYandexDirectBudgetProvider(
            oauth=_oauth(), transport=transport
        )
        minimum = provider.minimum_weekly_spend_limit_micros(
            access_token="token",
            currency="RUB",
            client_login="owner-login",
        )

        self.assertEqual(minimum, 300_000_000)
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertIn("/json/v501/dictionaries", str(call["url"]))
        body = json.loads(call["body"])
        self.assertEqual(body["method"], "get")
        self.assertEqual(body["params"]["DictionaryNames"], ["Currencies"])

    def test_default_goal_cap_uses_provider_minimum_visible_to_owner(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            hard_cap, daily_cap = goal._caps(
                100_000,
                minimum_weekly_spend_micros=300_000_000,
            )
        self.assertEqual(hard_cap, 30_000)
        self.assertEqual(daily_cap, 30_000)

    def test_explicit_operator_max_below_provider_minimum_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {"CLIENTPLATFORM_GOAL_MAX_SPEND_MINOR": "10000"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                AdSpendInvariantViolation,
                "configured goal spend maximum",
            ):
                goal._caps(
                    100_000,
                    minimum_weekly_spend_micros=300_000_000,
                )

    def test_explicit_daily_cap_cannot_make_weekly_budget_too_small(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CLIENTPLATFORM_GOAL_MAX_SPEND_MINOR": "50000",
                "CLIENTPLATFORM_GOAL_DAILY_SPEND_MINOR": "4000",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(
                AdSpendInvariantViolation,
                "daily spend maximum",
            ):
                goal._caps(
                    100_000,
                    minimum_weekly_spend_micros=300_000_000,
                )

    def test_available_balance_below_provider_minimum_fails_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                AdSpendInvariantViolation,
                "available Yandex budget",
            ):
                goal._caps(
                    20_000,
                    minimum_weekly_spend_micros=300_000_000,
                )


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
            expected_snapshot_strategy=managed_strategy_string(
                weekly_spend_limit_micros=None
            ),
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

    def test_new_consent_can_replace_the_exact_budget_it_observed(self) -> None:
        old_weekly = 50_000_000
        new_weekly = 70_000_000
        transport = FakeTransport(
            [
                _response(
                    {"result": {"Campaigns": [_managed_campaign(weekly=old_weekly)]}}
                ),
                _response({"result": {"UpdateResults": [{"Id": 6001}]}}),
                _response(
                    {"result": {"Campaigns": [_managed_campaign(weekly=new_weekly)]}}
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
            expected_snapshot_strategy=managed_strategy_string(
                weekly_spend_limit_micros=old_weekly
            ),
        )

        self.assertFalse(result.reconciled_without_mutation)
        self.assertEqual(result.weekly_spend_limit_micros, new_weekly)
        self.assertEqual(len(transport.calls), 3)

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
            expected_snapshot_strategy=managed_strategy_string(
                weekly_spend_limit_micros=None
            ),
        )
        self.assertTrue(result.reconciled_without_mutation)
        self.assertEqual(result.weekly_spend_limit_micros, weekly_micros)
        self.assertEqual(len(transport.calls), 1)

    def test_out_of_band_budget_drift_fails_before_write(self) -> None:
        drifted_weekly = 60_000_000
        transport = FakeTransport(
            [
                _response(
                    {
                        "result": {
                            "Campaigns": [
                                _managed_campaign(weekly=drifted_weekly)
                            ]
                        }
                    }
                )
            ]
        )
        provider = YandexDirectAdActions(oauth=_oauth(), transport=transport)
        with self.assertRaisesRegex(YandexDirectError, "budget_drift"):
            provider.configure_managed_launch_budget(
                access_token="token",
                external_campaign_id="6001",
                hard_cap_minor=9_000,
                daily_cap_minor=1_000,
                currency="RUB",
                expected_snapshot_strategy=managed_strategy_string(
                    weekly_spend_limit_micros=50_000_000
                ),
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

    def test_launch_runner_revalidates_consent_after_budget_write(self) -> None:
        source = inspect.getsource(process_one_ad_spend_operation)
        configure = source.index("configure_managed_launch_budget")
        post_write_guard = source.index("_current_launch_authorization(context)", configure)
        moderate = source.index("moderate_ad")
        self.assertLess(configure, post_write_guard)
        self.assertLess(post_write_guard, moderate)
        self.assertNotIn("resume_ad", source)


if __name__ == "__main__":
    unittest.main()

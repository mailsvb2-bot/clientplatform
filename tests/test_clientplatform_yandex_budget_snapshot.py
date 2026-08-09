from __future__ import annotations

import inspect
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from clientplatform.integrations.yandex_direct import (
    YandexDirectError,
    YandexOAuthConfig,
)
from clientplatform.integrations.yandex_direct_budget import (
    ReadOnlyYandexDirectBudgetProvider,
    YandexCampaignBudgetReadout,
    YandexDailySpendReadout,
    reconcile_yandex_budget_snapshot,
)


_NOW = datetime(2026, 8, 5, 17, 30, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
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


def _campaign_payload(
    *,
    funds=None,
    state: str = "ON",
    status: str = "ACCEPTED",
    status_payment: str = "ALLOWED",
    currency: str = "RUB",
):
    return {
        "result": {
            "Campaigns": [
                {
                    "Id": 6001,
                    "Type": "TEXT_CAMPAIGN",
                    "State": state,
                    "Status": status,
                    "StatusPayment": status_payment,
                    "Currency": currency,
                    "Funds": funds
                    or {
                        "Mode": "CAMPAIGN_FUNDS",
                        "CampaignFunds": {
                            "Sum": 500_000_000,
                            "Balance": 123_450_000,
                            "SumAvailableForTransfer": 100_000_000,
                        },
                    },
                    "DailyBudget": {"Amount": 50_000_000, "Mode": "STANDARD"},
                    "TextCampaign": {
                        "BiddingStrategy": {
                            "Search": {
                                "BiddingStrategyType": "HIGHEST_POSITION"
                            },
                            "Network": {
                                "BiddingStrategyType": "NETWORK_DEFAULT",
                                "NetworkDefault": {"LimitPercent": 100},
                            },
                        }
                    },
                }
            ]
        }
    }


def _provider(transport: FakeTransport) -> ReadOnlyYandexDirectBudgetProvider:
    return ReadOnlyYandexDirectBudgetProvider(
        oauth=YandexOAuthConfig(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://app.clientplatform.ru/oauth/yandex-direct/callback",
        ),
        transport=transport,
    )


def _readout(**overrides) -> YandexCampaignBudgetReadout:
    values = {
        "campaign_id": "6001",
        "currency": "RUB",
        "funds_mode": "CAMPAIGN_FUNDS",
        "available_budget_micros": 123_450_000,
        "total_spend_micros": None,
        "daily_budget_micros": 50_000_000,
        "campaign_type": "TEXT_CAMPAIGN",
        "state": "ON",
        "status": "ACCEPTED",
        "status_payment": "ALLOWED",
        "search_strategy": "HIGHEST_POSITION",
        "network_strategy": "NETWORK_DEFAULT",
        "captured_at": _NOW,
        "provider_version": "ycamp_" + "1" * 64,
    }
    values.update(overrides)
    return YandexCampaignBudgetReadout(**values)


def _spend(**overrides) -> YandexDailySpendReadout:
    values = {
        "campaign_id": "6001",
        "currency": "RUB",
        "report_date": "2026-08-05",
        "spend_micros": 12_340_000,
        "captured_at": _NOW,
        "provider_version": "yreport_" + "2" * 64,
    }
    values.update(overrides)
    return YandexDailySpendReadout(**values)


class YandexBudgetReaderTests(unittest.TestCase):
    def test_reads_campaign_and_daily_net_spend_without_mutation(self) -> None:
        campaign_response = json.dumps(_campaign_payload()).encode("utf-8")
        report_response = b"2026-08-05\t6001\t12340000\n"
        transport = FakeTransport(
            [
                (200, {}, campaign_response),
                (200, {}, report_response),
            ]
        )
        provider = _provider(transport)

        campaign = provider.campaign_budget_readout(
            access_token="private-token",
            external_campaign_id="6001",
            captured_at=_NOW,
            client_login="vasya",
        )
        daily = provider.daily_spend_readout(
            access_token="private-token",
            campaign=campaign,
            report_date=date(2026, 8, 5),
            captured_at=_NOW + timedelta(seconds=1),
            client_login="vasya",
        )
        snapshot = reconcile_yandex_budget_snapshot(
            connection_id=str(uuid4()),
            external_account_id="100500",
            campaign=campaign,
            daily_spend=daily,
            expected_report_date="2026-08-05",
            now=_NOW + timedelta(seconds=2),
        )

        self.assertTrue(campaign.launch_eligible)
        self.assertEqual(campaign.available_budget_micros, 123_450_000)
        self.assertEqual(campaign.daily_budget_micros, 50_000_000)
        self.assertEqual(daily.spend_micros, 12_340_000)
        self.assertEqual(snapshot.available_budget_minor, 12_345)
        self.assertEqual(snapshot.spent_today_minor, 1_234)
        self.assertTrue(snapshot.launch_eligible)
        self.assertIn("HIGHEST_POSITION", snapshot.strategy)

        campaign_call, report_call = transport.calls
        campaign_body = json.loads(campaign_call["body"])
        self.assertEqual(campaign_body["method"], "get")
        self.assertEqual(
            campaign_body["params"]["SelectionCriteria"],
            {"Ids": [6001]},
        )
        self.assertIn("Funds", campaign_body["params"]["FieldNames"])
        self.assertIn(
            "BiddingStrategy",
            campaign_body["params"]["TextCampaignFieldNames"],
        )
        self.assertTrue(str(campaign_call["url"]).endswith("/campaigns"))
        self.assertEqual(campaign_call["headers"]["Client-Login"], "vasya")

        report_body = json.loads(report_call["body"])
        report_params = report_body["params"]
        self.assertEqual(report_params["IncludeVAT"], "NO")
        self.assertEqual(report_params["DateRangeType"], "CUSTOM_DATE")
        self.assertEqual(report_params["FieldNames"], ["Date", "CampaignId", "Cost"])
        self.assertEqual(
            report_params["SelectionCriteria"]["Filter"][0]["Values"],
            ["6001"],
        )
        headers = report_call["headers"]
        self.assertEqual(headers["returnMoneyInMicros"], "true")
        self.assertEqual(headers["skipReportHeader"], "true")
        self.assertEqual(headers["Client-Login"], "vasya")
        self.assertNotIn("private-token", str(report_body))

    def test_empty_report_is_zero_but_pending_report_is_retryable(self) -> None:
        campaign = _readout()
        empty_transport = FakeTransport([(200, {}, b"")])
        daily = _provider(empty_transport).daily_spend_readout(
            access_token="private-token",
            campaign=campaign,
            report_date="2026-08-05",
            captured_at=_NOW,
        )
        self.assertEqual(daily.spend_micros, 0)

        pending = _provider(FakeTransport([(202, {}, b"")]))
        with self.assertRaises(YandexDirectError) as raised:
            pending.daily_spend_readout(
                access_token="private-token",
                campaign=campaign,
                report_date="2026-08-05",
                captured_at=_NOW,
            )
        self.assertEqual(raised.exception.code, "daily_spend_report_pending")
        self.assertTrue(raised.exception.retryable)

    def test_shared_account_is_read_but_cannot_be_reconciled_without_balance(self) -> None:
        response = _campaign_payload(
            funds={
                "Mode": "SHARED_ACCOUNT_FUNDS",
                "SharedAccountFunds": {"Spend": 900_000_000},
            }
        )
        transport = FakeTransport([(200, {}, json.dumps(response).encode("utf-8"))])
        campaign = _provider(transport).campaign_budget_readout(
            access_token="private-token",
            external_campaign_id="6001",
            captured_at=_NOW,
        )
        self.assertEqual(campaign.total_spend_micros, 900_000_000)
        self.assertIsNone(campaign.available_budget_micros)
        self.assertFalse(campaign.launch_eligible)
        with self.assertRaisesRegex(YandexDirectError, "shared_account_balance_unavailable"):
            reconcile_yandex_budget_snapshot(
                connection_id=str(uuid4()),
                external_account_id="100500",
                campaign=campaign,
                daily_spend=_spend(),
                expected_report_date="2026-08-05",
                now=_NOW,
            )

    def test_malformed_or_foreign_report_rows_fail_closed(self) -> None:
        provider = _provider(FakeTransport())
        with self.assertRaisesRegex(YandexDirectError, "daily_spend_report_invalid"):
            provider._parse_daily_spend_tsv(
                text="bad-row",
                campaign_id="6001",
                report_date="2026-08-05",
            )
        with self.assertRaisesRegex(
            YandexDirectError,
            "daily_spend_report_campaign_mismatch",
        ):
            provider._parse_daily_spend_tsv(
                text="2026-08-05\t7001\t1000000",
                campaign_id="6001",
                report_date="2026-08-05",
            )
        with self.assertRaisesRegex(
            YandexDirectError,
            "daily_spend_report_date_mismatch",
        ):
            provider._parse_daily_spend_tsv(
                text="2026-08-04\t6001\t1000000",
                campaign_id="6001",
                report_date="2026-08-05",
            )
        with self.assertRaisesRegex(YandexDirectError, "daily_spend_report_cost_invalid"):
            provider._parse_daily_spend_tsv(
                text="2026-08-05\t6001\t-1",
                campaign_id="6001",
                report_date="2026-08-05",
            )


class YandexBudgetReconciliationTests(unittest.TestCase):
    def test_reconciliation_rejects_inexact_money_and_mismatches(self) -> None:
        with self.assertRaisesRegex(YandexDirectError, "daily_spend_minor_unit_inexact"):
            reconcile_yandex_budget_snapshot(
                connection_id=str(uuid4()),
                external_account_id="100500",
                campaign=_readout(),
                daily_spend=_spend(spend_micros=1),
                expected_report_date="2026-08-05",
                now=_NOW,
            )
        with self.assertRaisesRegex(YandexDirectError, "campaign_mismatch"):
            reconcile_yandex_budget_snapshot(
                connection_id=str(uuid4()),
                external_account_id="100500",
                campaign=_readout(),
                daily_spend=_spend(campaign_id="7001"),
                expected_report_date="2026-08-05",
                now=_NOW,
            )
        with self.assertRaisesRegex(YandexDirectError, "date_mismatch"):
            reconcile_yandex_budget_snapshot(
                connection_id=str(uuid4()),
                external_account_id="100500",
                campaign=_readout(),
                daily_spend=_spend(report_date="2026-08-04"),
                expected_report_date="2026-08-05",
                now=_NOW,
            )

    def test_reconciliation_rejects_non_current_provider_day(self) -> None:
        with self.assertRaisesRegex(
            YandexDirectError,
            "budget_reconciliation_report_not_today",
        ):
            reconcile_yandex_budget_snapshot(
                connection_id=str(uuid4()),
                external_account_id="100500",
                campaign=_readout(),
                daily_spend=_spend(report_date="2026-08-06", spend_micros=0),
                expected_report_date="2026-08-06",
                now=_NOW,
                provider_timezone="Europe/Moscow",
            )

    def test_stale_and_future_provider_reads_fail_closed(self) -> None:
        with self.assertRaisesRegex(YandexDirectError, "campaign_read_stale"):
            reconcile_yandex_budget_snapshot(
                connection_id=str(uuid4()),
                external_account_id="100500",
                campaign=_readout(captured_at=_NOW - timedelta(seconds=121)),
                daily_spend=_spend(),
                expected_report_date="2026-08-05",
                now=_NOW,
            )
        with self.assertRaisesRegex(YandexDirectError, "daily_spend_read_from_future"):
            reconcile_yandex_budget_snapshot(
                connection_id=str(uuid4()),
                external_account_id="100500",
                campaign=_readout(),
                daily_spend=_spend(captured_at=_NOW + timedelta(seconds=6)),
                expected_report_date="2026-08-05",
                now=_NOW,
            )

    def test_ineligible_campaign_produces_non_launchable_snapshot(self) -> None:
        snapshot = reconcile_yandex_budget_snapshot(
            connection_id=str(uuid4()),
            external_account_id="100500",
            campaign=_readout(state="OFF"),
            daily_spend=_spend(),
            expected_report_date="2026-08-05",
            now=_NOW,
        )
        self.assertFalse(snapshot.launch_eligible)

    def test_adapter_source_contains_no_provider_mutations(self) -> None:
        source = inspect.getsource(ReadOnlyYandexDirectBudgetProvider)
        for forbidden in (
            '"method": "add"',
            '"method": "update"',
            '"method": "resume"',
            '"method": "moderate"',
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn('"method": "get"', source)
        self.assertNotIn("publish_text_ad", source)


if __name__ == "__main__":
    unittest.main()

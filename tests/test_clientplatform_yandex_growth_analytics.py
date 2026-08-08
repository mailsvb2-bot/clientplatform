from __future__ import annotations

import inspect
import json
import unittest
from datetime import date

from clientplatform.application.yandex_growth_analytics import (
    YandexGrowthCampaignSnapshot,
    YandexGrowthSnapshot,
    _current_period,
)
from clientplatform.integrations.yandex_direct import YandexDirectError, YandexOAuthConfig
from clientplatform.integrations.yandex_direct_analytics import (
    ReadOnlyYandexDirectAnalyticsProvider,
    YandexAdPerformanceRow,
)


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


def _provider(transport: FakeTransport) -> ReadOnlyYandexDirectAnalyticsProvider:
    return ReadOnlyYandexDirectAnalyticsProvider(
        oauth=YandexOAuthConfig(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://oauth.yandex.ru/verification_code",
        ),
        transport=transport,
    )


class YandexDirectAnalyticsProviderTests(unittest.TestCase):
    def test_exact_ad_report_uses_read_only_reports_contract(self) -> None:
        transport = FakeTransport(
            [
                (
                    200,
                    {},
                    (
                        "9001\t6001\tCampaign One\t1000\t25\t125000000\n"
                        "9002\t6001\tCampaign One\t500\t10\t25000000\n"
                    ).encode("utf-8"),
                )
            ]
        )
        report = _provider(transport).performance_report(
            access_token="private-token",
            ad_ids=("9002", "9001", "9001"),
            date_from="2026-07-11",
            date_to=date(2026, 8, 9),
            client_login=" owner-login ",
        )

        self.assertEqual(report.date_from, "2026-07-11")
        self.assertEqual(report.date_to, "2026-08-09")
        self.assertEqual(report.impressions, 1500)
        self.assertEqual(report.clicks, 35)
        self.assertEqual(report.cost_micros, 150_000_000)
        self.assertEqual(len(report.rows), 2)

        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertTrue(str(call["url"]).endswith("/json/v501/reports"))
        headers = call["headers"]
        self.assertEqual(headers["Authorization"], "Bearer private-token")
        self.assertEqual(headers["Client-Login"], "owner-login")
        self.assertEqual(headers["returnMoneyInMicros"], "true")
        self.assertEqual(headers["skipReportHeader"], "true")
        self.assertEqual(headers["skipColumnHeader"], "true")
        self.assertEqual(headers["skipReportSummary"], "true")

        body = json.loads(call["body"])
        params = body["params"]
        self.assertEqual(params["ReportType"], "AD_PERFORMANCE_REPORT")
        self.assertEqual(params["DateRangeType"], "CUSTOM_DATE")
        self.assertEqual(params["IncludeVAT"], "NO")
        self.assertEqual(params["IncludeDiscount"], "NO")
        self.assertEqual(
            params["FieldNames"],
            ["AdId", "CampaignId", "CampaignName", "Impressions", "Clicks", "Cost"],
        )
        self.assertEqual(
            params["SelectionCriteria"]["Filter"],
            [{"Field": "AdId", "Operator": "IN", "Values": ["9001", "9002"]}],
        )
        self.assertNotIn("private-token", str(body))

    def test_duplicate_report_rows_are_aggregated_per_exact_ad(self) -> None:
        rows = _provider(FakeTransport())._parse_rows(
            "9001\t6001\tCampaign\t10\t2\t3000000\n"
            "9001\t6001\tCampaign\t20\t3\t4000000\n"
        )
        self.assertEqual(
            rows,
            (
                YandexAdPerformanceRow(
                    ad_id="9001",
                    campaign_id="6001",
                    campaign_name="Campaign",
                    impressions=30,
                    clicks=5,
                    cost_micros=7_000_000,
                ),
            ),
        )

    def test_empty_selection_makes_no_provider_call(self) -> None:
        transport = FakeTransport()
        report = _provider(transport).performance_report(
            access_token="token",
            ad_ids=(),
            date_from="2026-08-03",
            date_to="2026-08-09",
        )
        self.assertEqual(report.rows, ())
        self.assertEqual(transport.calls, [])

    def test_pending_and_auth_failures_are_explicit(self) -> None:
        for status, code, retryable in (
            (202, "analytics_report_pending", True),
            (401, "provider_http_401", False),
            (429, "analytics_report_failed", True),
        ):
            with self.subTest(status=status):
                provider = _provider(FakeTransport([(status, {}, b"")]))
                with self.assertRaises(YandexDirectError) as raised:
                    provider.performance_report(
                        access_token="token",
                        ad_ids=("9001",),
                        date_from="2026-08-03",
                        date_to="2026-08-09",
                    )
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.retryable, retryable)

    def test_foreign_or_malformed_report_rows_fail_closed(self) -> None:
        provider = _provider(FakeTransport([(200, {}, b"9999\t6001\tX\t1\t1\t1\n")]))
        with self.assertRaisesRegex(YandexDirectError, "analytics_report_ad_mismatch"):
            provider.performance_report(
                access_token="token",
                ad_ids=("9001",),
                date_from="2026-08-03",
                date_to="2026-08-09",
            )
        for text in (
            "broken",
            "9001\t6001\tX\t-1\t1\t1",
            "abc\t6001\tX\t1\t1\t1",
        ):
            with self.subTest(text=text):
                with self.assertRaises(YandexDirectError):
                    _provider(FakeTransport())._parse_rows(text)

    def test_invalid_period_and_ad_count_fail_closed(self) -> None:
        provider = _provider(FakeTransport())
        with self.assertRaises(ValueError):
            provider.performance_report(
                access_token="token",
                ad_ids=("9001",),
                date_from="2026-08-10",
                date_to="2026-08-09",
            )
        with self.assertRaises(YandexDirectError):
            provider.performance_report(
                access_token="token",
                ad_ids=tuple(str(index) for index in range(1, 502)),
                date_from="2026-08-03",
                date_to="2026-08-09",
            )

    def test_adapter_does_not_expose_provider_mutation_methods(self) -> None:
        source = inspect.getsource(ReadOnlyYandexDirectAnalyticsProvider)
        for forbidden in (
            "publish_text_ad",
            '"method": "add"',
            '"method": "update"',
            '"method": "resume"',
            '"method": "moderate"',
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("AD_PERFORMANCE_REPORT", source)


class YandexGrowthValueTests(unittest.TestCase):
    def test_period_and_unit_metrics_are_deterministic(self) -> None:
        self.assertEqual(
            _current_period(7, now=date(2026, 8, 9)),
            ("2026-08-03", "2026-08-09"),
        )
        with self.assertRaises(ValueError):
            _current_period(14, now=date(2026, 8, 9))

        campaign = YandexGrowthCampaignSnapshot(
            connection_id="connection",
            campaign_id="6001",
            campaign_name="Campaign",
            tracked_ads=2,
            impressions=1000,
            clicks=100,
            cost_micros=50_000_000,
            leads=10,
            bookings=5,
            won=2,
        )
        self.assertEqual(campaign.ctr_percent, 10.0)
        self.assertEqual(campaign.cpc_micros, 500_000)
        self.assertEqual(campaign.cpl_micros, 5_000_000)
        self.assertEqual(campaign.booking_cost_micros, 10_000_000)
        self.assertEqual(campaign.cac_micros, 25_000_000)

        snapshot = YandexGrowthSnapshot(
            date_from="2026-07-11",
            date_to="2026-08-09",
            period_days=30,
            connected_accounts=1,
            tracked_ads=2,
            impressions=1000,
            clicks=100,
            cost_micros=50_000_000,
            leads=10,
            bookings=5,
            won=0,
            campaigns=(campaign,),
        )
        self.assertEqual(snapshot.ctr_percent, 10.0)
        self.assertEqual(snapshot.cpc_micros, 500_000)
        self.assertEqual(snapshot.cpl_micros, 5_000_000)
        self.assertEqual(snapshot.booking_cost_micros, 10_000_000)
        self.assertIsNone(snapshot.cac_micros)


if __name__ == "__main__":
    unittest.main()

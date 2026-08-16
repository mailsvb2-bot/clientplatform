from __future__ import annotations

import inspect
import json
import unittest
from datetime import date
from unittest.mock import patch

from clientplatform.application import yandex_campaign_diagnostics as diagnostics
from clientplatform.integrations.yandex_direct import YandexDirectError, YandexOAuthConfig
from clientplatform.integrations.yandex_direct_analytics import (
    ReadOnlyYandexDirectAnalyticsProvider,
    YandexCampaignPerformanceRow,
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


class CampaignReportProviderTests(unittest.TestCase):
    def test_campaign_report_uses_campaign_performance_contract(self) -> None:
        transport = FakeTransport(
            [
                (
                    200,
                    {},
                    (
                        "6001\tCampaign One\t1000\t25\t125000000\n"
                        "6002\tCampaign Two\t500\t10\t25000000\n"
                    ).encode("utf-8"),
                )
            ]
        )
        report = _provider(transport).campaign_performance_report(
            access_token="private-token",
            campaign_ids=("6002", "6001", "6001"),
            date_from="2026-08-03",
            date_to=date(2026, 8, 9),
            client_login=" owner-login ",
        )

        self.assertEqual(report.date_from, "2026-08-03")
        self.assertEqual(report.date_to, "2026-08-09")
        self.assertEqual(report.impressions, 1500)
        self.assertEqual(report.clicks, 35)
        self.assertEqual(report.cost_micros, 150_000_000)
        self.assertEqual([row.campaign_id for row in report.rows], ["6001", "6002"])

        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertTrue(str(call["url"]).endswith("/json/v501/reports"))
        self.assertEqual(call["headers"]["Authorization"], "Bearer private-token")
        self.assertEqual(call["headers"]["Client-Login"], "owner-login")
        self.assertEqual(call["headers"]["returnMoneyInMicros"], "true")
        body = json.loads(call["body"])
        params = body["params"]
        self.assertEqual(params["ReportType"], "CAMPAIGN_PERFORMANCE_REPORT")
        self.assertEqual(params["DateRangeType"], "CUSTOM_DATE")
        self.assertEqual(
            params["FieldNames"],
            ["CampaignId", "CampaignName", "Impressions", "Clicks", "Cost"],
        )
        self.assertEqual(
            params["SelectionCriteria"]["Filter"],
            [{"Field": "CampaignId", "Operator": "IN", "Values": ["6001", "6002"]}],
        )
        self.assertNotIn("private-token", str(body))

    def test_zero_rows_are_valid_provider_truth(self) -> None:
        transport = FakeTransport([(200, {}, b"")])
        report = _provider(transport).campaign_performance_report(
            access_token="token",
            campaign_ids=("6001",),
            date_from="2026-08-03",
            date_to="2026-08-09",
        )
        self.assertEqual(report.rows, ())
        self.assertEqual(report.cost_micros, 0)

    def test_foreign_duplicate_and_malformed_rows_fail_closed(self) -> None:
        provider = _provider(FakeTransport([(200, {}, b"9999\tForeign\t1\t1\t1\n")]))
        with self.assertRaisesRegex(YandexDirectError, "analytics_report_campaign_mismatch"):
            provider.campaign_performance_report(
                access_token="token",
                campaign_ids=("6001",),
                date_from="2026-08-03",
                date_to="2026-08-09",
            )

        for text, code in (
            ("broken", "analytics_report_invalid"),
            ("6001\tX\t-1\t1\t1", "impressions_invalid"),
            (
                "6001\tX\t1\t1\t1\n6001\tX\t2\t2\t2",
                "analytics_report_campaign_duplicate",
            ),
        ):
            with self.subTest(text=text):
                with self.assertRaisesRegex(YandexDirectError, code):
                    _provider(FakeTransport())._parse_campaign_rows(text)

    def test_pending_provider_and_token_failures_are_explicit(self) -> None:
        for status, code, retryable in (
            (201, "analytics_report_pending", True),
            (202, "analytics_report_pending", True),
            (400, "analytics_report_failed", False),
            (500, "analytics_report_failed", True),
        ):
            with self.subTest(status=status):
                with self.assertRaises(YandexDirectError) as raised:
                    _provider(FakeTransport([(status, {}, b"")])).campaign_performance_report(
                        access_token="token",
                        campaign_ids=("6001",),
                        date_from="2026-08-03",
                        date_to="2026-08-09",
                    )
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.retryable, retryable)

        with self.assertRaisesRegex(YandexDirectError, "provider_token_missing"):
            _provider(FakeTransport()).campaign_performance_report(
                access_token=" ",
                campaign_ids=("6001",),
                date_from="2026-08-03",
                date_to="2026-08-09",
            )

    def test_campaign_reader_remains_read_only(self) -> None:
        source = inspect.getsource(ReadOnlyYandexDirectAnalyticsProvider)
        self.assertIn("CAMPAIGN_PERFORMANCE_REPORT", source)
        for forbidden in (
            "publish_text_ad",
            '"method": "add"',
            '"method": "update"',
            '"method": "resume"',
            '"method": "moderate"',
        ):
            self.assertNotIn(forbidden, source)


class CampaignDiagnosticsTests(unittest.TestCase):
    def _tracked(
        self,
        campaign_id: str,
        *,
        connection_id: str = "connection-a",
        name: str = "Stored campaign",
    ) -> diagnostics._TrackedCampaign:
        return diagnostics._TrackedCampaign(
            connection_id=connection_id,
            external_login="owner-login",
            campaign_id=campaign_id,
            campaign_name=name,
        )

    def test_no_ad_id_is_not_a_requirement_for_managed_campaign_loading(self) -> None:
        source = inspect.getsource(diagnostics._load_managed_campaigns)
        self.assertIn("j.business_id=?", source)
        self.assertIn("current.assert_can_view_promotion_analytics()", source)
        self.assertIn("j.external_campaign_id IS NOT NULL", source)
        self.assertNotIn("j.external_ad_id", source)
        self.assertNotIn("status='submitted'", source)

    def test_missing_provider_row_keeps_managed_campaign_visible_with_zero_metrics(self) -> None:
        tracked = [self._tracked("6001")]
        with (
            patch.object(
                diagnostics,
                "_load_managed_campaigns",
                return_value=(object(), 1, tracked),
            ),
            patch.object(diagnostics, "_provider_rows", return_value={}),
        ):
            snapshot = diagnostics.get_yandex_campaign_diagnostics(
                actor=object(),
                period_days=7,
                now=date(2026, 8, 9),
                vault=object(),
                provider=object(),
            )
        self.assertEqual(snapshot.managed_campaigns, 1)
        self.assertEqual(snapshot.impressions, 0)
        self.assertEqual(snapshot.clicks, 0)
        self.assertEqual(snapshot.cost_micros, 0)
        self.assertEqual(snapshot.campaigns[0].campaign_id, "6001")
        self.assertFalse(snapshot.campaigns[0].has_provider_row)

    def test_provider_metrics_override_stored_name_without_business_attribution(self) -> None:
        tracked = [self._tracked("6001")]
        provider_rows = {
            ("connection-a", "6001"): YandexCampaignPerformanceRow(
                campaign_id="6001",
                campaign_name="Provider campaign",
                impressions=120,
                clicks=12,
                cost_micros=6_000_000,
            )
        }
        with (
            patch.object(
                diagnostics,
                "_load_managed_campaigns",
                return_value=(object(), 1, tracked),
            ),
            patch.object(diagnostics, "_provider_rows", return_value=provider_rows),
        ):
            snapshot = diagnostics.get_yandex_campaign_diagnostics(
                actor=object(),
                period_days=30,
                now=date(2026, 8, 9),
                vault=object(),
                provider=object(),
            )
        row = snapshot.campaigns[0]
        self.assertEqual(row.campaign_name, "Provider campaign")
        self.assertEqual(row.impressions, 120)
        self.assertEqual(row.clicks, 12)
        self.assertEqual(row.cost_micros, 6_000_000)
        self.assertTrue(row.has_provider_row)
        self.assertFalse(hasattr(snapshot, "leads"))
        self.assertFalse(hasattr(snapshot, "bookings"))
        self.assertFalse(hasattr(snapshot, "revenue"))

    def test_money_is_not_summed_across_connections_without_currency_identity(self) -> None:
        tracked = [
            self._tracked("6001", connection_id="connection-a"),
            self._tracked("7001", connection_id="connection-b"),
        ]
        provider_rows = {
            ("connection-a", "6001"): YandexCampaignPerformanceRow(
                campaign_id="6001",
                campaign_name="A",
                impressions=10,
                clicks=1,
                cost_micros=1_000_000,
            ),
            ("connection-b", "7001"): YandexCampaignPerformanceRow(
                campaign_id="7001",
                campaign_name="B",
                impressions=20,
                clicks=2,
                cost_micros=2_000_000,
            ),
        }
        with (
            patch.object(
                diagnostics,
                "_load_managed_campaigns",
                return_value=(object(), 2, tracked),
            ),
            patch.object(diagnostics, "_provider_rows", return_value=provider_rows),
        ):
            snapshot = diagnostics.get_yandex_campaign_diagnostics(
                actor=object(),
                period_days=30,
                now=date(2026, 8, 9),
                vault=object(),
                provider=object(),
            )
        self.assertEqual(snapshot.impressions, 30)
        self.assertEqual(snapshot.clicks, 3)
        self.assertIsNone(snapshot.cost_micros)
        self.assertIsNone(snapshot.cpc_micros)
        self.assertEqual([row.cost_micros for row in snapshot.campaigns], [1_000_000, 2_000_000])


if __name__ == "__main__":
    unittest.main()

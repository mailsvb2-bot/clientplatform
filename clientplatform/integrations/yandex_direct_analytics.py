from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date

from clientplatform.domain.ad_connections import normalize_external_campaign_id
from clientplatform.integrations.yandex_direct import (
    JsonHttpTransport,
    UrllibJsonTransport,
    YandexDirectError,
    YandexDirectProvider,
    YandexOAuthConfig,
    YandexTokenBundle,
)

_REPORTS_URL = "https://api.direct.yandex.com/json/v501/reports"
_MAX_AD_IDS = 500
_MAX_CAMPAIGN_IDS = 500
_MAX_METRIC_VALUE = 9_000_000_000_000_000_000


def _report_date(value: date | str, *, field_name: str) -> str:
    if isinstance(value, date):
        parsed = value
    else:
        try:
            parsed = date.fromisoformat(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"{field_name} must use YYYY-MM-DD") from exc
    return parsed.isoformat()


def _positive_external_id(value: object, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized.isascii() or not normalized.isdigit() or int(normalized) <= 0:
        raise YandexDirectError(f"{field_name}_invalid")
    return normalized


def _nonnegative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, (bool, float)):
        raise YandexDirectError(f"{field_name}_invalid")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise YandexDirectError(f"{field_name}_invalid") from exc
    if parsed < 0 or parsed > _MAX_METRIC_VALUE:
        raise YandexDirectError(f"{field_name}_invalid")
    return parsed


def _checked_add(left: int, right: int, *, field_name: str) -> int:
    result = int(left) + int(right)
    if result > _MAX_METRIC_VALUE:
        raise YandexDirectError(f"{field_name}_invalid")
    return result


def _clean_name(value: object) -> str:
    normalized = " ".join(str(value or "").replace("\x00", " ").split())
    return normalized[:255] or "Без названия"


def _report_headers(*, access_token: str, client_login: str) -> dict[str, str]:
    token = str(access_token or "").strip()
    if not token:
        raise YandexDirectError("provider_token_missing")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
        "returnMoneyInMicros": "true",
        "skipReportHeader": "true",
        "skipColumnHeader": "true",
        "skipReportSummary": "true",
    }
    normalized_login = " ".join(str(client_login or "").split())
    if normalized_login:
        headers["Client-Login"] = normalized_login
    return headers


@dataclass(frozen=True, slots=True)
class YandexAdPerformanceRow:
    ad_id: str
    campaign_id: str
    campaign_name: str
    impressions: int
    clicks: int
    cost_micros: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "ad_id",
            _positive_external_id(self.ad_id, field_name="ad_id"),
        )
        object.__setattr__(
            self,
            "campaign_id",
            normalize_external_campaign_id(self.campaign_id),
        )
        object.__setattr__(self, "campaign_name", _clean_name(self.campaign_name))
        for name in ("impressions", "clicks", "cost_micros"):
            object.__setattr__(
                self,
                name,
                _nonnegative_int(getattr(self, name), field_name=name),
            )


@dataclass(frozen=True, slots=True)
class YandexAdPerformanceReport:
    date_from: str
    date_to: str
    rows: tuple[YandexAdPerformanceRow, ...]

    @property
    def impressions(self) -> int:
        return sum(row.impressions for row in self.rows)

    @property
    def clicks(self) -> int:
        return sum(row.clicks for row in self.rows)

    @property
    def cost_micros(self) -> int:
        return sum(row.cost_micros for row in self.rows)


@dataclass(frozen=True, slots=True)
class YandexCampaignPerformanceRow:
    campaign_id: str
    campaign_name: str
    impressions: int
    clicks: int
    cost_micros: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "campaign_id",
            normalize_external_campaign_id(self.campaign_id),
        )
        object.__setattr__(self, "campaign_name", _clean_name(self.campaign_name))
        for name in ("impressions", "clicks", "cost_micros"):
            object.__setattr__(
                self,
                name,
                _nonnegative_int(getattr(self, name), field_name=name),
            )


@dataclass(frozen=True, slots=True)
class YandexCampaignPerformanceReport:
    date_from: str
    date_to: str
    rows: tuple[YandexCampaignPerformanceRow, ...]

    @property
    def impressions(self) -> int:
        return sum(row.impressions for row in self.rows)

    @property
    def clicks(self) -> int:
        return sum(row.clicks for row in self.rows)

    @property
    def cost_micros(self) -> int:
        return sum(row.cost_micros for row in self.rows)


class ReadOnlyYandexDirectAnalyticsProvider:
    """Read-only Yandex Reports adapter for exact ads and managed campaigns.

    The adapter deliberately exposes reports and OAuth refresh only. Campaign
    diagnostics never inherit mutation, spend, bidding, moderation or launch
    methods from the publishing provider.
    """

    def __init__(
        self,
        *,
        oauth: YandexOAuthConfig,
        transport: JsonHttpTransport | None = None,
    ) -> None:
        oauth.validate()
        self._transport = transport or UrllibJsonTransport()
        self._oauth = YandexDirectProvider(oauth=oauth, transport=self._transport)

    def refresh(self, *, bundle: YandexTokenBundle) -> YandexTokenBundle:
        return self._oauth.refresh(bundle=bundle)

    def performance_report(
        self,
        *,
        access_token: str,
        ad_ids: tuple[str, ...],
        date_from: date | str,
        date_to: date | str,
        client_login: str = "",
    ) -> YandexAdPerformanceReport:
        start = _report_date(date_from, field_name="date_from")
        end = _report_date(date_to, field_name="date_to")
        if start > end:
            raise ValueError("date_from must not be after date_to")
        normalized_ids = tuple(
            sorted(
                {
                    _positive_external_id(value, field_name="ad_id")
                    for value in ad_ids
                },
                key=int,
            )
        )
        if not normalized_ids:
            return YandexAdPerformanceReport(date_from=start, date_to=end, rows=())
        if len(normalized_ids) > _MAX_AD_IDS:
            raise YandexDirectError("analytics_ad_limit_exceeded")

        fingerprint = hashlib.sha256(
            (start + "|" + end + "|" + ",".join(normalized_ids)).encode("ascii")
        ).hexdigest()[:16]
        payload = {
            "params": {
                "SelectionCriteria": {
                    "DateFrom": start,
                    "DateTo": end,
                    "Filter": [
                        {
                            "Field": "AdId",
                            "Operator": "IN",
                            "Values": list(normalized_ids),
                        }
                    ],
                },
                "FieldNames": [
                    "AdId",
                    "CampaignId",
                    "CampaignName",
                    "Impressions",
                    "Clicks",
                    "Cost",
                ],
                "ReportName": f"clientplatform-growth-{fingerprint}",
                "ReportType": "AD_PERFORMANCE_REPORT",
                "DateRangeType": "CUSTOM_DATE",
                "Format": "TSV",
                "IncludeVAT": "NO",
                "IncludeDiscount": "NO",
            }
        }
        status, _response_headers, raw = self._transport.request(
            method="POST",
            url=_REPORTS_URL,
            headers=_report_headers(
                access_token=access_token,
                client_login=client_login,
            ),
            body=json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            timeout=20.0,
        )
        if status in {201, 202}:
            raise YandexDirectError("analytics_report_pending", retryable=True)
        if status == 401:
            raise YandexDirectError("provider_http_401")
        if status != 200:
            raise YandexDirectError(
                "analytics_report_failed",
                retryable=status in {408, 425, 429} or status >= 500,
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise YandexDirectError("analytics_report_invalid") from exc
        rows = self._parse_rows(text)
        requested = set(normalized_ids)
        if any(row.ad_id not in requested for row in rows):
            raise YandexDirectError("analytics_report_ad_mismatch")
        return YandexAdPerformanceReport(date_from=start, date_to=end, rows=rows)

    def campaign_performance_report(
        self,
        *,
        access_token: str,
        campaign_ids: tuple[str, ...],
        date_from: date | str,
        date_to: date | str,
        client_login: str = "",
    ) -> YandexCampaignPerformanceReport:
        """Read provider truth by CampaignId without requiring a published AdId."""

        start = _report_date(date_from, field_name="date_from")
        end = _report_date(date_to, field_name="date_to")
        if start > end:
            raise ValueError("date_from must not be after date_to")
        normalized_ids = tuple(
            sorted(
                {normalize_external_campaign_id(value) for value in campaign_ids},
                key=int,
            )
        )
        if not normalized_ids:
            return YandexCampaignPerformanceReport(
                date_from=start,
                date_to=end,
                rows=(),
            )
        if len(normalized_ids) > _MAX_CAMPAIGN_IDS:
            raise YandexDirectError("analytics_campaign_limit_exceeded")

        fingerprint = hashlib.sha256(
            (
                "campaign|"
                + start
                + "|"
                + end
                + "|"
                + ",".join(normalized_ids)
            ).encode("ascii")
        ).hexdigest()[:16]
        payload = {
            "params": {
                "SelectionCriteria": {
                    "DateFrom": start,
                    "DateTo": end,
                    "Filter": [
                        {
                            "Field": "CampaignId",
                            "Operator": "IN",
                            "Values": list(normalized_ids),
                        }
                    ],
                },
                "FieldNames": [
                    "CampaignId",
                    "CampaignName",
                    "Impressions",
                    "Clicks",
                    "Cost",
                ],
                "ReportName": f"clientplatform-campaign-{fingerprint}",
                "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
                "DateRangeType": "CUSTOM_DATE",
                "Format": "TSV",
                "IncludeVAT": "NO",
                "IncludeDiscount": "NO",
            }
        }
        status, _response_headers, raw = self._transport.request(
            method="POST",
            url=_REPORTS_URL,
            headers=_report_headers(
                access_token=access_token,
                client_login=client_login,
            ),
            body=json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            timeout=20.0,
        )
        if status in {201, 202}:
            raise YandexDirectError("analytics_report_pending", retryable=True)
        if status == 401:
            raise YandexDirectError("provider_http_401")
        if status != 200:
            raise YandexDirectError(
                "analytics_report_failed",
                retryable=status in {408, 425, 429} or status >= 500,
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise YandexDirectError("analytics_report_invalid") from exc
        rows = self._parse_campaign_rows(text)
        requested = set(normalized_ids)
        if any(row.campaign_id not in requested for row in rows):
            raise YandexDirectError("analytics_report_campaign_mismatch")
        return YandexCampaignPerformanceReport(
            date_from=start,
            date_to=end,
            rows=rows,
        )

    @staticmethod
    def _parse_rows(text: str) -> tuple[YandexAdPerformanceRow, ...]:
        aggregated: dict[tuple[str, str], tuple[str, int, int, int]] = {}
        campaign_by_ad: dict[str, str] = {}
        for raw_line in text.splitlines():
            if not raw_line.strip():
                continue
            columns = raw_line.split("\t")
            if len(columns) != 6:
                raise YandexDirectError("analytics_report_invalid")
            ad_id, campaign_id, campaign_name, impressions, clicks, cost = (
                column.strip() for column in columns
            )
            normalized_ad = _positive_external_id(ad_id, field_name="ad_id")
            normalized_campaign = normalize_external_campaign_id(campaign_id)
            previous_campaign = campaign_by_ad.setdefault(
                normalized_ad,
                normalized_campaign,
            )
            if previous_campaign != normalized_campaign:
                raise YandexDirectError("analytics_report_campaign_ambiguous")
            key = (normalized_ad, normalized_campaign)
            clean_name = _clean_name(campaign_name)
            previous = aggregated.get(key)
            if previous is None:
                aggregated[key] = (
                    clean_name,
                    _nonnegative_int(impressions, field_name="impressions"),
                    _nonnegative_int(clicks, field_name="clicks"),
                    _nonnegative_int(cost, field_name="cost_micros"),
                )
                continue
            previous_name, previous_impressions, previous_clicks, previous_cost = previous
            if previous_name != clean_name:
                raise YandexDirectError("analytics_report_campaign_name_ambiguous")
            aggregated[key] = (
                previous_name,
                _checked_add(
                    previous_impressions,
                    _nonnegative_int(impressions, field_name="impressions"),
                    field_name="impressions",
                ),
                _checked_add(
                    previous_clicks,
                    _nonnegative_int(clicks, field_name="clicks"),
                    field_name="clicks",
                ),
                _checked_add(
                    previous_cost,
                    _nonnegative_int(cost, field_name="cost_micros"),
                    field_name="cost_micros",
                ),
            )
        return tuple(
            YandexAdPerformanceRow(
                ad_id=key[0],
                campaign_id=key[1],
                campaign_name=values[0],
                impressions=values[1],
                clicks=values[2],
                cost_micros=values[3],
            )
            for key, values in sorted(
                aggregated.items(),
                key=lambda item: int(item[0][0]),
            )
        )

    @staticmethod
    def _parse_campaign_rows(text: str) -> tuple[YandexCampaignPerformanceRow, ...]:
        rows: dict[str, YandexCampaignPerformanceRow] = {}
        for raw_line in text.splitlines():
            if not raw_line.strip():
                continue
            columns = raw_line.split("\t")
            if len(columns) != 5:
                raise YandexDirectError("analytics_report_invalid")
            campaign_id, campaign_name, impressions, clicks, cost = (
                column.strip() for column in columns
            )
            normalized_campaign = normalize_external_campaign_id(campaign_id)
            if normalized_campaign in rows:
                raise YandexDirectError("analytics_report_campaign_duplicate")
            rows[normalized_campaign] = YandexCampaignPerformanceRow(
                campaign_id=normalized_campaign,
                campaign_name=campaign_name,
                impressions=_nonnegative_int(impressions, field_name="impressions"),
                clicks=_nonnegative_int(clicks, field_name="clicks"),
                cost_micros=_nonnegative_int(cost, field_name="cost_micros"),
            )
        return tuple(rows[key] for key in sorted(rows, key=int))


__all__ = [
    "ReadOnlyYandexDirectAnalyticsProvider",
    "YandexAdPerformanceReport",
    "YandexAdPerformanceRow",
    "YandexCampaignPerformanceReport",
    "YandexCampaignPerformanceRow",
]

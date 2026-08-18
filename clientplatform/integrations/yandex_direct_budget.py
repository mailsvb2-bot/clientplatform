from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.domain.ad_connections import (
    AdProvider,
    normalize_external_campaign_id,
)
from clientplatform.domain.ad_spend import ProviderBudgetSnapshot
from clientplatform.domain.tenancy import normalize_uuid
from clientplatform.integrations.yandex_direct import (
    JsonHttpTransport,
    YandexDirectError,
    YandexOAuthConfig,
)
from clientplatform.integrations.yandex_direct_moderation import (
    ModeratingYandexDirectProvider,
)

_REPORTS_URL = "https://api.direct.yandex.com/json/v501/reports"
_SUPPORTED_CURRENCY_MICRO_TO_MINOR = {
    "BYN": 10_000,
    "CHF": 10_000,
    "EUR": 10_000,
    "KZT": 10_000,
    "RUB": 10_000,
    "TRY": 10_000,
    "UAH": 10_000,
    "USD": 10_000,
}


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash(prefix: str, value: object) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return prefix + digest


def _timestamp(value: datetime | str, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | str, name: str) -> str:
    return _timestamp(value, name).isoformat(timespec="seconds")


def _report_date(value: date | str) -> str:
    if isinstance(value, datetime):
        raise ValueError("report_date must not include time")
    if isinstance(value, date):
        parsed = value
    else:
        try:
            parsed = date.fromisoformat(str(value).strip())
        except ValueError as exc:
            raise ValueError("report_date must use YYYY-MM-DD") from exc
    return parsed.isoformat()


def _provider_day(now: datetime | str, timezone_name: str) -> str:
    zone_name = str(timezone_name or "").strip()
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise YandexDirectError("report_timezone_invalid") from exc
    return _timestamp(now, "now").astimezone(zone).date().isoformat()


def _token(value: object, name: str, limit: int = 160) -> str:
    token = str(value or "").strip().upper()
    if not token or len(token) > limit or "\x00" in token:
        raise YandexDirectError(f"{name}_invalid")
    return token


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, (bool, float)):
        raise YandexDirectError(f"{name}_invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise YandexDirectError(f"{name}_invalid") from exc
    if parsed < 0 or parsed > 9_000_000_000_000_000_000:
        raise YandexDirectError(f"{name}_invalid")
    return parsed


def _optional_nonnegative_int(value: object, name: str) -> int | None:
    if value in (None, ""):
        return None
    return _nonnegative_int(value, name)


def _mapping(value: object, error_code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise YandexDirectError(error_code)
    return value


@dataclass(frozen=True, slots=True)
class YandexCampaignBudgetReadout:
    campaign_id: str
    currency: str
    funds_mode: str
    available_budget_micros: int | None
    total_spend_micros: int | None
    daily_budget_micros: int | None
    campaign_type: str
    state: str
    status: str
    status_payment: str
    search_strategy: str
    network_strategy: str
    captured_at: str
    provider_version: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "campaign_id",
            normalize_external_campaign_id(self.campaign_id),
        )
        currency = str(self.currency or "").strip().upper()
        if currency not in _SUPPORTED_CURRENCY_MICRO_TO_MINOR:
            raise YandexDirectError("campaign_currency_unsupported")
        object.__setattr__(self, "currency", currency)
        for name in (
            "funds_mode",
            "campaign_type",
            "state",
            "status",
            "status_payment",
            "search_strategy",
            "network_strategy",
        ):
            object.__setattr__(self, name, _token(getattr(self, name), name))
        for name in (
            "available_budget_micros",
            "total_spend_micros",
            "daily_budget_micros",
        ):
            object.__setattr__(
                self,
                name,
                _optional_nonnegative_int(getattr(self, name), name),
            )
        object.__setattr__(self, "captured_at", _iso(self.captured_at, "captured_at"))
        version = str(self.provider_version or "").strip()
        if not version.startswith("ycamp_") or len(version) != 70:
            raise YandexDirectError("campaign_provider_version_invalid")

    @property
    def launch_eligible(self) -> bool:
        funded = (
            self.status_payment == "ALLOWED"
            and self.funds_mode == "CAMPAIGN_FUNDS"
            and self.available_budget_micros is not None
            and self.available_budget_micros > 0
            and self.search_strategy != "UNKNOWN"
            and self.network_strategy != "UNKNOWN"
        )
        legacy_ready = (
            self.campaign_type == "TEXT_CAMPAIGN"
            and self.state == "ON"
            and self.status == "ACCEPTED"
        )
        managed_draft_ready = (
            self.campaign_type == "UNIFIED_CAMPAIGN"
            and self.state == "OFF"
            and self.status == "DRAFT"
            and self.search_strategy == "HIGHEST_POSITION"
            and self.network_strategy == "SERVING_OFF"
        )
        return funded and (legacy_ready or managed_draft_ready)


@dataclass(frozen=True, slots=True)
class YandexDailySpendReadout:
    campaign_id: str
    currency: str
    report_date: str
    spend_micros: int
    captured_at: str
    provider_version: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "campaign_id",
            normalize_external_campaign_id(self.campaign_id),
        )
        currency = str(self.currency or "").strip().upper()
        if currency not in _SUPPORTED_CURRENCY_MICRO_TO_MINOR:
            raise YandexDirectError("report_currency_unsupported")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "report_date", _report_date(self.report_date))
        object.__setattr__(
            self,
            "spend_micros",
            _nonnegative_int(self.spend_micros, "report_spend_micros"),
        )
        object.__setattr__(self, "captured_at", _iso(self.captured_at, "captured_at"))
        version = str(self.provider_version or "").strip()
        if not version.startswith("yreport_") or len(version) != 72:
            raise YandexDirectError("report_provider_version_invalid")


class ReadOnlyYandexDirectBudgetProvider(ModeratingYandexDirectProvider):
    """Read-only financial adapter for consent-bound Yandex Direct spending.

    It uses only Campaigns.get and the Reports service. It never calls provider
    mutation methods and never turns a DRAFT into an active advertisement.
    """

    def __init__(
        self,
        *,
        oauth: YandexOAuthConfig,
        transport: JsonHttpTransport | None = None,
    ) -> None:
        super().__init__(oauth=oauth, transport=transport)

    def _client_scoped_direct_call(
        self,
        *,
        service: str,
        token: str,
        payload: Mapping[str, Any],
        client_login: str = "",
    ) -> Mapping[str, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        }
        normalized_login = " ".join(str(client_login or "").split())
        if normalized_login:
            headers["Client-Login"] = normalized_login
        response = self._json_or_error(
            method="POST",
            url=f"{self.MANAGED_API_ROOT}/{service}",
            headers=headers,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise YandexDirectError("provider_result_missing")
        return result

    def campaign_budget_readout(
        self,
        *,
        access_token: str,
        external_campaign_id: str,
        captured_at: datetime | str,
        client_login: str = "",
    ) -> YandexCampaignBudgetReadout:
        campaign_id = normalize_external_campaign_id(external_campaign_id)
        result = self._client_scoped_direct_call(
            service="campaigns",
            token=access_token,
            client_login=client_login,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {"Ids": [int(campaign_id)]},
                    "FieldNames": [
                        "Id",
                        "Type",
                        "State",
                        "Status",
                        "StatusPayment",
                        "Currency",
                        "Funds",
                        "DailyBudget",
                    ],
                    "TextCampaignFieldNames": ["BiddingStrategy"],
                    "UnifiedCampaignFieldNames": ["BiddingStrategy"],
                },
            },
        )
        campaigns = result.get("Campaigns") or []
        if len(campaigns) != 1 or not isinstance(campaigns[0], Mapping):
            raise YandexDirectError("campaign_budget_identity_ambiguous")
        item = campaigns[0]
        if normalize_external_campaign_id(item.get("Id")) != campaign_id:
            raise YandexDirectError("campaign_budget_identity_mismatch")

        campaign_type = _token(item.get("Type"), "campaign_type")
        funds = _mapping(item.get("Funds"), "campaign_funds_missing")
        mode = _token(funds.get("Mode"), "campaign_funds_mode")
        available: int | None = None
        total_spend: int | None = None
        if mode == "CAMPAIGN_FUNDS":
            campaign_funds = _mapping(
                funds.get("CampaignFunds"),
                "campaign_funds_missing",
            )
            available = _nonnegative_int(
                campaign_funds.get("Balance"),
                "campaign_balance_micros",
            )
        elif mode == "SHARED_ACCOUNT_FUNDS":
            shared = _mapping(
                funds.get("SharedAccountFunds"),
                "shared_account_funds_missing",
            )
            total_spend = _nonnegative_int(
                shared.get("Spend"),
                "shared_account_spend_micros",
            )
        else:
            raise YandexDirectError("campaign_funds_mode_unsupported")

        daily_budget_raw = item.get("DailyBudget")
        daily_budget = None
        if daily_budget_raw is not None:
            daily_budget_mapping = _mapping(
                daily_budget_raw,
                "campaign_daily_budget_invalid",
            )
            daily_budget = _nonnegative_int(
                daily_budget_mapping.get("Amount"),
                "campaign_daily_budget_micros",
            )

        if campaign_type == "TEXT_CAMPAIGN":
            campaign_settings = _mapping(
                item.get("TextCampaign"),
                "text_campaign_settings_missing",
            )
        elif campaign_type == "UNIFIED_CAMPAIGN":
            campaign_settings = _mapping(
                item.get("UnifiedCampaign"),
                "unified_campaign_settings_missing",
            )
        else:
            raise YandexDirectError("campaign_type_unsupported")
        bidding = _mapping(
            campaign_settings.get("BiddingStrategy"),
            "campaign_bidding_strategy_missing",
        )
        search = _mapping(
            bidding.get("Search"),
            "campaign_search_strategy_missing",
        )
        network = _mapping(
            bidding.get("Network"),
            "campaign_network_strategy_missing",
        )
        selected = {
            "Id": item.get("Id"),
            "Type": item.get("Type"),
            "State": item.get("State"),
            "Status": item.get("Status"),
            "StatusPayment": item.get("StatusPayment"),
            "Currency": item.get("Currency"),
            "Funds": item.get("Funds"),
            "DailyBudget": item.get("DailyBudget"),
            "BiddingStrategy": campaign_settings.get("BiddingStrategy"),
        }
        return YandexCampaignBudgetReadout(
            campaign_id=campaign_id,
            currency=_token(item.get("Currency"), "campaign_currency"),
            funds_mode=mode,
            available_budget_micros=available,
            total_spend_micros=total_spend,
            daily_budget_micros=daily_budget,
            campaign_type=campaign_type,
            state=_token(item.get("State"), "campaign_state"),
            status=_token(item.get("Status"), "campaign_status"),
            status_payment=_token(
                item.get("StatusPayment"),
                "campaign_status_payment",
            ),
            search_strategy=_token(
                search.get("BiddingStrategyType") or "UNKNOWN",
                "campaign_search_strategy",
            ),
            network_strategy=_token(
                network.get("BiddingStrategyType") or "UNKNOWN",
                "campaign_network_strategy",
            ),
            captured_at=captured_at,
            provider_version=_hash("ycamp_", selected),
        )

    def daily_spend_readout(
        self,
        *,
        access_token: str,
        campaign: YandexCampaignBudgetReadout,
        report_date: date | str,
        captured_at: datetime | str,
        client_login: str = "",
    ) -> YandexDailySpendReadout:
        day = _report_date(report_date)
        campaign_id = campaign.campaign_id
        report_name = f"clientplatform-{campaign_id}-{day}-net-spend"
        payload = {
            "params": {
                "SelectionCriteria": {
                    "DateFrom": day,
                    "DateTo": day,
                    "Filter": [
                        {
                            "Field": "CampaignId",
                            "Operator": "IN",
                            "Values": [campaign_id],
                        }
                    ],
                },
                "FieldNames": ["Date", "CampaignId", "Cost"],
                "ReportName": report_name,
                "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
                "DateRangeType": "CUSTOM_DATE",
                "Format": "TSV",
                "IncludeVAT": "NO",
                "IncludeDiscount": "NO",
            }
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
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
        status, _response_headers, raw = self._transport.request(
            method="POST",
            url=_REPORTS_URL,
            headers=headers,
            body=_canonical_json(payload).encode("utf-8"),
            timeout=20.0,
        )
        if status in {201, 202}:
            raise YandexDirectError("daily_spend_report_pending", retryable=True)
        if status != 200:
            raise YandexDirectError(
                "daily_spend_report_failed",
                retryable=status in {408, 425, 429} or status >= 500,
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise YandexDirectError("daily_spend_report_invalid") from exc
        spend = self._parse_daily_spend_tsv(
            text=text,
            campaign_id=campaign_id,
            report_date=day,
        )
        version_payload = {
            "request": payload,
            "response_sha256": hashlib.sha256(raw).hexdigest(),
        }
        return YandexDailySpendReadout(
            campaign_id=campaign_id,
            currency=campaign.currency,
            report_date=day,
            spend_micros=spend,
            captured_at=captured_at,
            provider_version=_hash("yreport_", version_payload),
        )

    @staticmethod
    def _parse_daily_spend_tsv(
        *,
        text: str,
        campaign_id: str,
        report_date: str,
    ) -> int:
        total = 0
        rows = [line for line in text.splitlines() if line.strip()]
        for line in rows:
            columns = line.split("\t")
            if len(columns) != 3:
                raise YandexDirectError("daily_spend_report_invalid")
            row_date, row_campaign_id, raw_cost = (item.strip() for item in columns)
            if row_date != report_date:
                raise YandexDirectError("daily_spend_report_date_mismatch")
            if normalize_external_campaign_id(row_campaign_id) != campaign_id:
                raise YandexDirectError("daily_spend_report_campaign_mismatch")
            total += _nonnegative_int(raw_cost, "daily_spend_report_cost")
            if total > 9_000_000_000_000_000_000:
                raise YandexDirectError("daily_spend_report_cost_invalid")
        return total


def reconcile_yandex_budget_snapshot(
    *,
    connection_id: str,
    external_account_id: str,
    campaign: YandexCampaignBudgetReadout,
    daily_spend: YandexDailySpendReadout,
    expected_report_date: date | str,
    now: datetime | str,
    provider_timezone: str = "Europe/Moscow",
    max_read_age_seconds: int = 120,
    validity_seconds: int = 60,
) -> ProviderBudgetSnapshot:
    """Create a short-lived domain snapshot from two independent read-only reads."""

    normalized_connection = normalize_uuid(connection_id, field_name="connection_id")
    account_id = str(external_account_id or "").strip()
    if not account_id or len(account_id) > 255 or "\x00" in account_id:
        raise YandexDirectError("direct_account_id_invalid")
    expected_day = _report_date(expected_report_date)
    current = _timestamp(now, "now")
    if expected_day != _provider_day(current, provider_timezone):
        raise YandexDirectError("budget_reconciliation_report_not_today")
    if isinstance(max_read_age_seconds, bool) or not 1 <= int(max_read_age_seconds) <= 600:
        raise ValueError("max_read_age_seconds must be between 1 and 600")
    if isinstance(validity_seconds, bool) or not 1 <= int(validity_seconds) <= 300:
        raise ValueError("validity_seconds must be between 1 and 300")
    if campaign.campaign_id != daily_spend.campaign_id:
        raise YandexDirectError("budget_reconciliation_campaign_mismatch")
    if campaign.currency != daily_spend.currency:
        raise YandexDirectError("budget_reconciliation_currency_mismatch")
    if daily_spend.report_date != expected_day:
        raise YandexDirectError("budget_reconciliation_date_mismatch")
    if campaign.funds_mode != "CAMPAIGN_FUNDS":
        raise YandexDirectError("shared_account_balance_unavailable")
    if campaign.available_budget_micros is None:
        raise YandexDirectError("campaign_available_budget_missing")

    max_age = timedelta(seconds=int(max_read_age_seconds))
    future_tolerance = timedelta(seconds=5)
    for name, captured_raw in (
        ("campaign", campaign.captured_at),
        ("daily_spend", daily_spend.captured_at),
    ):
        captured = _timestamp(captured_raw, f"{name}_captured_at")
        if captured > current + future_tolerance:
            raise YandexDirectError(f"{name}_read_from_future")
        if current - captured > max_age:
            raise YandexDirectError(f"{name}_read_stale")

    available_minor = _micros_to_minor_exact(
        campaign.available_budget_micros,
        campaign.currency,
        "campaign_available_budget",
    )
    spent_today_minor = _micros_to_minor_exact(
        daily_spend.spend_micros,
        campaign.currency,
        "daily_spend",
    )
    combined_version = _hash(
        "ybudget_",
        {
            "campaign": campaign.provider_version,
            "daily_spend": daily_spend.provider_version,
            "expected_report_date": expected_day,
        },
    )
    return ProviderBudgetSnapshot(
        provider=AdProvider.YANDEX_DIRECT,
        connection_id=normalized_connection,
        external_account_id=account_id,
        external_campaign_id=campaign.campaign_id,
        currency=campaign.currency,
        available_budget_minor=available_minor,
        spent_today_minor=spent_today_minor,
        campaign_status=(
            f"{campaign.campaign_type}:{campaign.state}:"
            f"{campaign.status}:{campaign.status_payment}"
        ),
        strategy=(
            f"search={campaign.search_strategy};"
            f"network={campaign.network_strategy}"
        ),
        launch_eligible=campaign.launch_eligible,
        provider_version=combined_version,
        captured_at=current,
        valid_until=current + timedelta(seconds=int(validity_seconds)),
    )


def _micros_to_minor_exact(value: int, currency: str, name: str) -> int:
    divisor = _SUPPORTED_CURRENCY_MICRO_TO_MINOR.get(currency)
    if divisor is None:
        raise YandexDirectError("currency_minor_unit_unsupported")
    amount = _nonnegative_int(value, f"{name}_micros")
    quotient, remainder = divmod(amount, divisor)
    if remainder:
        raise YandexDirectError(f"{name}_minor_unit_inexact")
    return quotient


__all__ = [
    "ReadOnlyYandexDirectBudgetProvider",
    "YandexCampaignBudgetReadout",
    "YandexDailySpendReadout",
    "reconcile_yandex_budget_snapshot",
]

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from clientplatform.domain.ad_connections import normalize_external_campaign_id
from clientplatform.integrations.yandex_direct import (
    JsonHttpTransport,
    YandexDirectError,
    YandexDirectProvider,
    YandexOAuthConfig,
)

_YANDEX_MICROS_PER_MINOR = 10_000
_SUPPORTED_CURRENCIES = {"BYN", "CHF", "EUR", "KZT", "RUB", "TRY", "UAH", "USD"}


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _timestamp(value: datetime | str) -> str:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("captured_at must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _positive_id(value: object, name: str) -> str:
    raw = str(value or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        raise YandexDirectError(f"{name}_invalid")
    return raw


def _positive_minor(value: object, name: str) -> int:
    if isinstance(value, (bool, float)):
        raise YandexDirectError(f"{name}_invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise YandexDirectError(f"{name}_invalid") from exc
    if parsed <= 0 or parsed > 900_000_000_000_000:
        raise YandexDirectError(f"{name}_invalid")
    return parsed


def _token(value: object, name: str) -> str:
    token = str(value or "").strip().upper()
    if not token or len(token) > 160 or "\x00" in token:
        raise YandexDirectError(f"{name}_invalid")
    return token


def _mapping(value: object, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise YandexDirectError(code)
    return value


@dataclass(frozen=True, slots=True)
class YandexAdState:
    ad_id: str
    ad_group_id: str
    campaign_id: str
    state: str
    status: str
    ad_type: str
    captured_at: str
    provider_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "ad_id", _positive_id(self.ad_id, "ad_id"))
        object.__setattr__(
            self,
            "ad_group_id",
            _positive_id(self.ad_group_id, "ad_group_id"),
        )
        object.__setattr__(
            self,
            "campaign_id",
            normalize_external_campaign_id(self.campaign_id),
        )
        for name in ("state", "status", "ad_type"):
            object.__setattr__(self, name, _token(getattr(self, name), name))
        object.__setattr__(self, "captured_at", _timestamp(self.captured_at))
        if not str(self.provider_version).startswith("yad_") or len(
            str(self.provider_version)
        ) != 68:
            raise YandexDirectError("ad_provider_version_invalid")

    @property
    def is_draft(self) -> bool:
        return self.status == "DRAFT"

    @property
    def is_submitted(self) -> bool:
        return self.status in {"MODERATION", "PREACCEPTED", "ACCEPTED"}

    @property
    def is_suspended(self) -> bool:
        return self.state == "SUSPENDED"


@dataclass(frozen=True, slots=True)
class YandexAdActionResult:
    operation: str
    before: YandexAdState
    after: YandexAdState
    reconciled_without_mutation: bool


@dataclass(frozen=True, slots=True)
class YandexManagedActivationResult:
    campaign_id: str
    campaign_type: str
    weekly_spend_limit_micros: int | None
    reconciled_without_mutation: bool


class YandexDirectAdActions(YandexDirectProvider):
    """Exact-ID operations only; never resumes or broadens advertising."""

    def __init__(
        self,
        *,
        oauth: YandexOAuthConfig,
        transport: JsonHttpTransport | None = None,
    ) -> None:
        super().__init__(oauth=oauth, transport=transport)

    def _call_at_root(
        self,
        *,
        root: str,
        service: str,
        access_token: str,
        payload: Mapping[str, Any],
        client_login: str = "",
    ) -> Mapping[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        }
        normalized_login = " ".join(str(client_login or "").split())
        if normalized_login:
            headers["Client-Login"] = normalized_login
        response = self._json_or_error(
            method="POST",
            url=f"{root}/{service}",
            headers=headers,
            body=_canonical_json(payload).encode("utf-8"),
        )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise YandexDirectError("provider_result_missing")
        return result

    def _call(
        self,
        *,
        access_token: str,
        payload: Mapping[str, Any],
        client_login: str = "",
    ) -> Mapping[str, Any]:
        # v501 is required for unified campaigns and remains the canonical
        # endpoint for exact-ID ad state/moderation in the managed lifecycle.
        return self._call_at_root(
            root=self.MANAGED_API_ROOT,
            service="ads",
            access_token=access_token,
            payload=payload,
            client_login=client_login,
        )

    def _managed_campaign_state(
        self,
        *,
        access_token: str,
        external_campaign_id: str,
        client_login: str = "",
    ) -> Mapping[str, Any]:
        campaign_id = normalize_external_campaign_id(external_campaign_id)
        result = self._call_at_root(
            root=self.MANAGED_API_ROOT,
            service="campaigns",
            access_token=access_token,
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
                    ],
                    "UnifiedCampaignFieldNames": ["BiddingStrategy"],
                },
            },
        )
        campaigns = result.get("Campaigns") or []
        if len(campaigns) != 1 or not isinstance(campaigns[0], Mapping):
            raise YandexDirectError("campaign_activation_identity_ambiguous")
        item = campaigns[0]
        if normalize_external_campaign_id(item.get("Id")) != campaign_id:
            raise YandexDirectError("campaign_activation_identity_mismatch")
        return item

    @staticmethod
    def _managed_weekly_limit(item: Mapping[str, Any]) -> int | None:
        unified = _mapping(item.get("UnifiedCampaign"), "managed_campaign_settings_missing")
        bidding = _mapping(
            unified.get("BiddingStrategy"),
            "managed_campaign_bidding_strategy_missing",
        )
        search = _mapping(
            bidding.get("Search"),
            "managed_campaign_search_strategy_missing",
        )
        network = _mapping(
            bidding.get("Network"),
            "managed_campaign_network_strategy_missing",
        )
        if _token(search.get("BiddingStrategyType"), "managed_search_strategy") != "HIGHEST_POSITION":
            raise YandexDirectError("managed_campaign_search_strategy_unsafe")
        if _token(network.get("BiddingStrategyType"), "managed_network_strategy") != "SERVING_OFF":
            raise YandexDirectError("managed_campaign_network_strategy_unsafe")
        highest = search.get("HighestPosition")
        if highest in (None, ""):
            return None
        highest_mapping = _mapping(highest, "managed_highest_position_invalid")
        raw = highest_mapping.get("WeeklySpendLimit")
        if raw in (None, ""):
            return None
        if isinstance(raw, (bool, float)):
            raise YandexDirectError("managed_weekly_spend_limit_invalid")
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise YandexDirectError("managed_weekly_spend_limit_invalid") from exc
        if value <= 0:
            raise YandexDirectError("managed_weekly_spend_limit_invalid")
        return value

    def configure_managed_launch_budget(
        self,
        *,
        access_token: str,
        external_campaign_id: str,
        hard_cap_minor: int,
        daily_cap_minor: int,
        currency: str,
        client_login: str = "",
    ) -> YandexManagedActivationResult:
        """Apply a consent-bounded provider budget to a managed draft.

        Legacy TEXT_CAMPAIGN objects are deliberately left unchanged. A unified
        campaign is accepted only from the exact non-serving baseline created by
        the managed publication flow. The provider weekly cap can never exceed
        either the total hard cap or seven approved daily caps.
        """
        campaign_id = normalize_external_campaign_id(external_campaign_id)
        item = self._managed_campaign_state(
            access_token=access_token,
            external_campaign_id=campaign_id,
            client_login=client_login,
        )
        campaign_type = _token(item.get("Type"), "campaign_type")
        if campaign_type != "UNIFIED_CAMPAIGN":
            return YandexManagedActivationResult(
                campaign_id=campaign_id,
                campaign_type=campaign_type,
                weekly_spend_limit_micros=None,
                reconciled_without_mutation=True,
            )
        if _token(item.get("State"), "campaign_state") != "OFF":
            raise YandexDirectError("managed_campaign_not_draft_off")
        if _token(item.get("Status"), "campaign_status") != "DRAFT":
            raise YandexDirectError("managed_campaign_not_draft_off")
        if _token(item.get("StatusPayment"), "campaign_status_payment") != "ALLOWED":
            raise YandexDirectError("managed_campaign_payment_not_allowed")
        provider_currency = _token(item.get("Currency"), "campaign_currency")
        expected_currency = _token(currency, "authorization_currency")
        if provider_currency != expected_currency or provider_currency not in _SUPPORTED_CURRENCIES:
            raise YandexDirectError("managed_campaign_currency_mismatch")

        hard_cap = _positive_minor(hard_cap_minor, "hard_cap_minor")
        daily_cap = _positive_minor(daily_cap_minor, "daily_cap_minor")
        weekly_minor = min(hard_cap, daily_cap * 7)
        weekly_micros = weekly_minor * _YANDEX_MICROS_PER_MINOR
        observed_limit = self._managed_weekly_limit(item)
        if observed_limit == weekly_micros:
            return YandexManagedActivationResult(
                campaign_id=campaign_id,
                campaign_type=campaign_type,
                weekly_spend_limit_micros=weekly_micros,
                reconciled_without_mutation=True,
            )
        if observed_limit is not None:
            raise YandexDirectError("managed_campaign_budget_drift")

        result = self._call_at_root(
            root=self.MANAGED_API_ROOT,
            service="campaigns",
            access_token=access_token,
            client_login=client_login,
            payload={
                "method": "update",
                "params": {
                    "Campaigns": [
                        {
                            "Id": int(campaign_id),
                            "UnifiedCampaign": {
                                "BiddingStrategy": {
                                    "Search": {
                                        "BiddingStrategyType": "HIGHEST_POSITION",
                                        "HighestPosition": {
                                            "WeeklySpendLimit": weekly_micros
                                        },
                                    },
                                    "Network": {
                                        "BiddingStrategyType": "SERVING_OFF"
                                    },
                                }
                            },
                        }
                    ]
                },
            },
        )
        entries = result.get("UpdateResults") or []
        if (
            len(entries) != 1
            or not isinstance(entries[0], Mapping)
            or entries[0].get("Errors")
        ):
            raise YandexDirectError("managed_campaign_budget_update_failed")
        if _positive_id(entries[0].get("Id"), "provider_action_id") != campaign_id:
            raise YandexDirectError("provider_action_identity_mismatch")

        after = self._managed_campaign_state(
            access_token=access_token,
            external_campaign_id=campaign_id,
            client_login=client_login,
        )
        if _token(after.get("Type"), "campaign_type") != "UNIFIED_CAMPAIGN":
            raise YandexDirectError("managed_campaign_type_drift")
        if _token(after.get("State"), "campaign_state") != "OFF" or _token(
            after.get("Status"), "campaign_status"
        ) != "DRAFT":
            raise YandexDirectError("managed_campaign_state_drift")
        if self._managed_weekly_limit(after) != weekly_micros:
            raise YandexDirectError("managed_campaign_budget_not_reconciled", retryable=True)
        return YandexManagedActivationResult(
            campaign_id=campaign_id,
            campaign_type=campaign_type,
            weekly_spend_limit_micros=weekly_micros,
            reconciled_without_mutation=False,
        )

    def ad_state(
        self,
        *,
        access_token: str,
        external_ad_id: str,
        captured_at: datetime | str,
        client_login: str = "",
    ) -> YandexAdState:
        ad_id = _positive_id(external_ad_id, "ad_id")
        result = self._call(
            access_token=access_token,
            client_login=client_login,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {"Ids": [int(ad_id)]},
                    "FieldNames": [
                        "Id",
                        "AdGroupId",
                        "CampaignId",
                        "State",
                        "Status",
                        "Type",
                    ],
                },
            },
        )
        ads = result.get("Ads") or []
        if len(ads) != 1 or not isinstance(ads[0], Mapping):
            raise YandexDirectError("ad_identity_ambiguous")
        item = ads[0]
        if _positive_id(item.get("Id"), "ad_id") != ad_id:
            raise YandexDirectError("ad_identity_mismatch")
        selected = {
            key: item.get(key)
            for key in (
                "Id",
                "AdGroupId",
                "CampaignId",
                "State",
                "Status",
                "Type",
            )
        }
        return YandexAdState(
            ad_id=ad_id,
            ad_group_id=_positive_id(item.get("AdGroupId"), "ad_group_id"),
            campaign_id=normalize_external_campaign_id(item.get("CampaignId")),
            state=_token(item.get("State"), "ad_state"),
            status=_token(item.get("Status"), "ad_status"),
            ad_type=_token(item.get("Type"), "ad_type"),
            captured_at=captured_at,
            provider_version=(
                "yad_"
                + hashlib.sha256(
                    _canonical_json(selected).encode("utf-8")
                ).hexdigest()
            ),
        )

    def _action(
        self,
        *,
        method: str,
        result_key: str,
        access_token: str,
        external_ad_id: str,
        expected_campaign_id: str,
        captured_at: datetime | str,
        client_login: str = "",
    ) -> YandexAdActionResult:
        before = self.ad_state(
            access_token=access_token,
            external_ad_id=external_ad_id,
            captured_at=captured_at,
            client_login=client_login,
        )
        if before.campaign_id != normalize_external_campaign_id(
            expected_campaign_id
        ):
            raise YandexDirectError("ad_campaign_identity_mismatch")

        if method == "moderate" and before.is_submitted:
            return YandexAdActionResult(method, before, before, True)
        if method == "suspend" and (before.is_suspended or before.is_draft):
            return YandexAdActionResult(method, before, before, True)
        if method == "moderate" and not before.is_draft:
            raise YandexDirectError("ad_not_launchable")

        result = self._call(
            access_token=access_token,
            client_login=client_login,
            payload={
                "method": method,
                "params": {
                    "SelectionCriteria": {"Ids": [int(before.ad_id)]}
                },
            },
        )
        entries = result.get(result_key) or []
        if (
            len(entries) != 1
            or not isinstance(entries[0], Mapping)
            or entries[0].get("Errors")
        ):
            raise YandexDirectError(f"ad_{method}_failed")
        if _positive_id(
            entries[0].get("Id"),
            "provider_action_id",
        ) != before.ad_id:
            raise YandexDirectError("provider_action_identity_mismatch")

        after = self.ad_state(
            access_token=access_token,
            external_ad_id=before.ad_id,
            captured_at=captured_at,
            client_login=client_login,
        )
        if method == "moderate" and not after.is_submitted:
            raise YandexDirectError(
                "ad_moderation_not_reconciled",
                retryable=True,
            )
        if method == "suspend" and not after.is_suspended:
            raise YandexDirectError(
                "ad_suspension_not_reconciled",
                retryable=True,
            )
        return YandexAdActionResult(method, before, after, False)

    def moderate_ad(self, **kwargs: Any) -> YandexAdActionResult:
        return self._action(
            method="moderate",
            result_key="ModerateResults",
            **kwargs,
        )

    def suspend_ad(self, **kwargs: Any) -> YandexAdActionResult:
        return self._action(
            method="suspend",
            result_key="SuspendResults",
            **kwargs,
        )


__all__ = [
    "YandexAdActionResult",
    "YandexAdState",
    "YandexDirectAdActions",
    "YandexManagedActivationResult",
]

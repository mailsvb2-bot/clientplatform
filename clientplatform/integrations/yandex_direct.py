from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from clientplatform.domain.ad_connections import (
    normalize_external_campaign_id,
    normalize_region_ids,
    pkce_challenge,
)
from clientplatform.domain.managed_ad_campaigns import normalize_managed_campaign_name


_YANDEX_MOSCOW = timezone(timedelta(hours=3))


class YandexDirectError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(str(code))
        self.code = _safe_code(code)
        self.retryable = bool(retryable)


class JsonHttpTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout: float = 20.0,
    ) -> tuple[int, Mapping[str, str], bytes]: ...


class UrllibJsonTransport:
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout: float = 20.0,
    ) -> tuple[int, Mapping[str, str], bytes]:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed provider URLs
                return int(response.status), dict(response.headers.items()), response.read()
        except HTTPError as exc:
            return int(exc.code), dict(exc.headers.items()), exc.read()
        except (URLError, TimeoutError, socket.timeout) as exc:
            raise YandexDirectError("provider_transport_unavailable", retryable=True) from exc


@dataclass(frozen=True, slots=True)
class YandexOAuthConfig:
    client_id: str
    redirect_uri: str
    client_secret: str = ""

    def validate(self) -> None:
        if not self.client_id.strip():
            raise ValueError("Yandex OAuth client id is required")
        if not self.redirect_uri.startswith("https://"):
            raise ValueError("Yandex OAuth redirect URI must use HTTPS")


@dataclass(frozen=True, slots=True)
class YandexTokenBundle:
    access_token: str
    token_type: str
    expires_in: int | None
    refresh_token: str | None
    scope: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "token_type": self.token_type,
                "expires_in": self.expires_in,
                "refresh_token": self.refresh_token,
                "scope": list(self.scope),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> "YandexTokenBundle":
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise YandexDirectError("credential_bundle_invalid") from exc
        token = str(data.get("access_token") or "").strip()
        if not token:
            raise YandexDirectError("credential_access_token_missing")
        refresh = str(data.get("refresh_token") or "").strip() or None
        expires_raw = data.get("expires_in")
        expires = None if expires_raw in (None, "") else int(expires_raw)
        scope_raw = data.get("scope") or []
        if isinstance(scope_raw, str):
            scope = tuple(item for item in scope_raw.replace(",", " ").split() if item)
        else:
            scope = tuple(str(item) for item in scope_raw if str(item).strip())
        return cls(
            access_token=token,
            token_type=str(data.get("token_type") or "bearer"),
            expires_in=expires,
            refresh_token=refresh,
            scope=scope,
        )


@dataclass(frozen=True, slots=True)
class YandexAccountIdentity:
    account_id: str
    login: str


@dataclass(frozen=True, slots=True)
class YandexCampaign:
    campaign_id: str
    name: str
    state: str
    status: str
    campaign_type: str


@dataclass(frozen=True, slots=True)
class YandexPublicationResult:
    ad_group_id: str
    ad_id: str


def _campaign_start_date(value: date | str | None) -> str:
    if value is None:
        return datetime.now(_YANDEX_MOSCOW).date().isoformat()
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    raw = str(value or "").strip()
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise ValueError("Yandex campaign start_date must use YYYY-MM-DD") from exc


class YandexDirectProvider:
    AUTHORIZE_URL = "https://oauth.yandex.com/authorize"
    TOKEN_URL = "https://oauth.yandex.com/token"
    PROFILE_URL = "https://login.yandex.ru/info?format=json"
    API_ROOT = "https://api.direct.yandex.com/json/v5"
    MANAGED_API_ROOT = "https://api.direct.yandex.com/json/v501"

    def __init__(
        self,
        *,
        oauth: YandexOAuthConfig,
        transport: JsonHttpTransport | None = None,
    ) -> None:
        oauth.validate()
        self._oauth = oauth
        self._transport = transport or UrllibJsonTransport()

    def authorization_url(self, *, state: str, verifier: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self._oauth.client_id,
            "redirect_uri": self._oauth.redirect_uri,
            "force_confirm": "yes",
            "state": state,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        return self.AUTHORIZE_URL + "?" + urlencode(params)

    def exchange_code(self, *, code: str, verifier: str) -> YandexTokenBundle:
        confirmation_code = str(code or "").strip()
        if not confirmation_code or len(confirmation_code) > 2048:
            raise YandexDirectError("oauth_code_invalid")
        fields = {
            "grant_type": "authorization_code",
            "code": confirmation_code,
            "client_id": self._oauth.client_id,
            "redirect_uri": self._oauth.redirect_uri,
            "code_verifier": verifier,
        }
        if self._oauth.client_secret:
            fields["client_secret"] = self._oauth.client_secret
        payload = self._json_or_error(
            method="POST",
            url=self.TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urlencode(fields).encode("ascii"),
            oauth_call=True,
        )
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise YandexDirectError("oauth_access_token_missing")
        scope_raw = payload.get("scope") or []
        if isinstance(scope_raw, str):
            scopes = tuple(item for item in scope_raw.replace(",", " ").split() if item)
        else:
            scopes = tuple(str(item) for item in scope_raw if str(item).strip())
        expires_raw = payload.get("expires_in")
        return YandexTokenBundle(
            access_token=token,
            token_type=str(payload.get("token_type") or "bearer"),
            expires_in=None if expires_raw in (None, "") else int(expires_raw),
            refresh_token=str(payload.get("refresh_token") or "").strip() or None,
            scope=scopes,
        )

    def refresh(self, *, bundle: YandexTokenBundle) -> YandexTokenBundle:
        if not bundle.refresh_token:
            raise YandexDirectError("oauth_refresh_token_missing")
        fields = {
            "grant_type": "refresh_token",
            "refresh_token": bundle.refresh_token,
            "client_id": self._oauth.client_id,
        }
        if self._oauth.client_secret:
            fields["client_secret"] = self._oauth.client_secret
        payload = self._json_or_error(
            method="POST",
            url=self.TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urlencode(fields).encode("ascii"),
            oauth_call=True,
        )
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise YandexDirectError("oauth_access_token_missing")
        refresh_token = str(payload.get("refresh_token") or "").strip() or bundle.refresh_token
        expires_raw = payload.get("expires_in")
        scope_raw = payload.get("scope") or bundle.scope
        if isinstance(scope_raw, str):
            scopes = tuple(item for item in scope_raw.replace(",", " ").split() if item)
        else:
            scopes = tuple(str(item) for item in scope_raw if str(item).strip())
        return YandexTokenBundle(
            access_token=access_token,
            token_type=str(payload.get("token_type") or bundle.token_type),
            expires_in=None if expires_raw in (None, "") else int(expires_raw),
            refresh_token=refresh_token,
            scope=scopes,
        )

    def account_identity(self, *, access_token: str) -> YandexAccountIdentity:
        payload = self._json_or_error(
            method="GET",
            url=self.PROFILE_URL,
            headers={"Authorization": f"OAuth {access_token}"},
        )
        account_id = str(payload.get("id") or payload.get("client_id") or "").strip()
        login = str(payload.get("login") or payload.get("default_email") or "").strip()
        if not account_id or not login:
            raise YandexDirectError("oauth_account_identity_missing")
        return YandexAccountIdentity(account_id=account_id, login=login)

    def list_text_campaigns(self, *, access_token: str) -> list[YandexCampaign]:
        result = self._direct_call(
            service="campaigns",
            token=access_token,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {"Types": ["TEXT_CAMPAIGN"], "States": ["ON", "OFF", "SUSPENDED"]},
                    "FieldNames": ["Id", "Name", "State", "Status", "Type"],
                },
            },
        )
        campaigns = []
        for item in result.get("Campaigns") or []:
            campaign_type = str(item.get("Type") or "")
            if campaign_type != "TEXT_CAMPAIGN":
                continue
            campaigns.append(
                YandexCampaign(
                    campaign_id=normalize_external_campaign_id(item.get("Id")),
                    name=" ".join(str(item.get("Name") or "Без названия").split())[:255],
                    state=str(item.get("State") or "UNKNOWN"),
                    status=str(item.get("Status") or "UNKNOWN"),
                    campaign_type=campaign_type,
                )
            )
        return campaigns

    def find_managed_campaign(
        self,
        *,
        access_token: str,
        campaign_name: str,
    ) -> YandexCampaign | None:
        expected_name = normalize_managed_campaign_name(campaign_name)
        result = self._managed_direct_call(
            service="campaigns",
            token=access_token,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {
                        "Types": ["UNIFIED_CAMPAIGN"],
                        "States": ["ON", "OFF", "SUSPENDED"],
                    },
                    "FieldNames": ["Id", "Name", "State", "Status", "Type"],
                },
            },
        )
        matches: list[YandexCampaign] = []
        for item in result.get("Campaigns") or []:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("Type") or "").strip().upper() != "UNIFIED_CAMPAIGN":
                continue
            if " ".join(str(item.get("Name") or "").split()) != expected_name:
                continue
            matches.append(
                YandexCampaign(
                    campaign_id=normalize_external_campaign_id(item.get("Id")),
                    name=expected_name,
                    state=str(item.get("State") or "UNKNOWN").strip().upper(),
                    status=str(item.get("Status") or "UNKNOWN").strip().upper(),
                    campaign_type="UNIFIED_CAMPAIGN",
                )
            )
        if len(matches) > 1:
            raise YandexDirectError("managed_campaign_marker_ambiguous")
        return matches[0] if matches else None

    def create_disabled_managed_campaign(
        self,
        *,
        access_token: str,
        campaign_name: str,
        start_date: date | str | None = None,
    ) -> str:
        expected_name = normalize_managed_campaign_name(campaign_name)
        result = self._managed_direct_call(
            service="campaigns",
            token=access_token,
            payload={
                "method": "add",
                "params": {
                    "Campaigns": [
                        {
                            "Name": expected_name,
                            "StartDate": _campaign_start_date(start_date),
                            "UnifiedCampaign": {
                                "BiddingStrategy": {
                                    "Search": {"BiddingStrategyType": "SERVING_OFF"},
                                    "Network": {"BiddingStrategyType": "SERVING_OFF"},
                                }
                            },
                        }
                    ]
                },
            },
        )
        return str(
            _first_add_id(
                result,
                key="AddResults",
                error_code="managed_campaign_creation_failed",
            )
        )

    def publish_managed_text_ad(
        self,
        *,
        access_token: str,
        external_campaign_id: str,
        expected_campaign_name: str,
        region_ids: tuple[int, ...],
        title: str,
        text: str,
        href: str,
        idempotency_key: str,
    ) -> YandexPublicationResult:
        campaign_id = int(normalize_external_campaign_id(external_campaign_id))
        expected_name = normalize_managed_campaign_name(expected_campaign_name)
        self._assert_managed_campaign_non_serving(
            access_token=access_token,
            campaign_id=campaign_id,
            expected_campaign_name=expected_name,
        )
        regions = list(normalize_region_ids(region_ids))
        destination = str(href or "").strip()
        if not destination.startswith("https://"):
            raise YandexDirectError("destination_url_invalid")
        group_name = f"ClientPlatform {idempotency_key}"[:255]
        group_id = self._find_managed_group(
            access_token=access_token,
            campaign_id=campaign_id,
            group_name=group_name,
        ) or self._add_managed_group(
            access_token=access_token,
            campaign_id=campaign_id,
            group_name=group_name,
            region_ids=regions,
        )
        ad_id = self._find_managed_ad(
            access_token=access_token,
            ad_group_id=group_id,
            href=destination,
        ) or self._add_managed_ad(
            access_token=access_token,
            ad_group_id=group_id,
            title=" ".join(str(title or "").split())[:56],
            text=" ".join(str(text or "").split())[:75],
            href=destination,
        )
        return YandexPublicationResult(ad_group_id=str(group_id), ad_id=str(ad_id))

    def _assert_managed_campaign_non_serving(
        self,
        *,
        access_token: str,
        campaign_id: int,
        expected_campaign_name: str,
    ) -> None:
        result = self._managed_direct_call(
            service="campaigns",
            token=access_token,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {"Ids": [campaign_id]},
                    "FieldNames": ["Id", "Name", "Type"],
                    "UnifiedCampaignFieldNames": ["BiddingStrategy"],
                },
            },
        )
        campaigns = result.get("Campaigns") or []
        if len(campaigns) != 1 or not isinstance(campaigns[0], Mapping):
            raise YandexDirectError("managed_campaign_not_found")
        item = campaigns[0]
        if normalize_external_campaign_id(item.get("Id")) != str(campaign_id):
            raise YandexDirectError("managed_campaign_identity_mismatch")
        if " ".join(str(item.get("Name") or "").split()) != expected_campaign_name:
            raise YandexDirectError("managed_campaign_name_mismatch")
        if str(item.get("Type") or "").strip().upper() != "UNIFIED_CAMPAIGN":
            raise YandexDirectError("managed_campaign_type_mismatch")
        unified = item.get("UnifiedCampaign")
        strategy = unified.get("BiddingStrategy") if isinstance(unified, Mapping) else None
        if not isinstance(strategy, Mapping):
            raise YandexDirectError("managed_campaign_strategy_missing")
        for placement in ("Search", "Network"):
            placement_strategy = strategy.get(placement)
            if not isinstance(placement_strategy, Mapping):
                raise YandexDirectError("managed_campaign_strategy_missing")
            if (
                str(placement_strategy.get("BiddingStrategyType") or "")
                .strip()
                .upper()
                != "SERVING_OFF"
            ):
                raise YandexDirectError("managed_campaign_serving_is_enabled")

    def _find_managed_group(
        self,
        *,
        access_token: str,
        campaign_id: int,
        group_name: str,
    ) -> int | None:
        result = self._managed_direct_call(
            service="adgroups",
            token=access_token,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {"CampaignIds": [campaign_id]},
                    "FieldNames": ["Id", "Name", "CampaignId", "Type"],
                },
            },
        )
        matches = [
            int(item["Id"])
            for item in result.get("AdGroups") or []
            if isinstance(item, Mapping)
            and str(item.get("Name") or "") == group_name
            and str(item.get("Type") or "").strip().upper() == "UNIFIED_AD_GROUP"
        ]
        if len(matches) > 1:
            raise YandexDirectError("managed_ad_group_marker_ambiguous")
        return matches[0] if matches else None

    def _add_managed_group(
        self,
        *,
        access_token: str,
        campaign_id: int,
        group_name: str,
        region_ids: list[int],
    ) -> int:
        result = self._managed_direct_call(
            service="adgroups",
            token=access_token,
            payload={
                "method": "add",
                "params": {
                    "AdGroups": [
                        {
                            "Name": group_name,
                            "CampaignId": campaign_id,
                            "RegionIds": region_ids,
                            "UnifiedAdGroup": {"OfferRetargeting": "NO"},
                        }
                    ]
                },
            },
        )
        return _first_add_id(
            result,
            key="AddResults",
            error_code="managed_ad_group_creation_failed",
        )

    def _find_managed_ad(
        self,
        *,
        access_token: str,
        ad_group_id: int,
        href: str,
    ) -> int | None:
        result = self._managed_direct_call(
            service="ads",
            token=access_token,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {"AdGroupIds": [ad_group_id]},
                    "FieldNames": ["Id", "AdGroupId", "Type"],
                    "TextAdFieldNames": ["Href", "Title", "Text"],
                },
            },
        )
        matches = []
        for item in result.get("Ads") or []:
            if not isinstance(item, Mapping):
                continue
            text_ad = item.get("TextAd") or {}
            if isinstance(text_ad, Mapping) and str(text_ad.get("Href") or "") == href:
                matches.append(int(item["Id"]))
        if len(matches) > 1:
            raise YandexDirectError("managed_ad_marker_ambiguous")
        return matches[0] if matches else None

    def _add_managed_ad(
        self,
        *,
        access_token: str,
        ad_group_id: int,
        title: str,
        text: str,
        href: str,
    ) -> int:
        if not title or not text:
            raise YandexDirectError("ad_copy_empty")
        result = self._managed_direct_call(
            service="ads",
            token=access_token,
            payload={
                "method": "add",
                "params": {
                    "Ads": [
                        {
                            "AdGroupId": ad_group_id,
                            "TextAd": {
                                "Title": title,
                                "Text": text,
                                "Href": href,
                                "Mobile": "NO",
                            },
                        }
                    ]
                },
            },
        )
        return _first_add_id(
            result,
            key="AddResults",
            error_code="managed_ad_creation_failed",
        )

    def publish_text_ad(
        self,
        *,
        access_token: str,
        external_campaign_id: str,
        region_ids: tuple[int, ...],
        title: str,
        text: str,
        href: str,
        idempotency_key: str,
    ) -> YandexPublicationResult:
        campaign_id = int(normalize_external_campaign_id(external_campaign_id))
        regions = list(normalize_region_ids(region_ids))
        destination = str(href or "").strip()
        if not destination.startswith("https://"):
            raise YandexDirectError("destination_url_invalid")
        group_name = f"ClientPlatform {idempotency_key}"[:255]
        existing_group = self._find_group(
            access_token=access_token,
            campaign_id=campaign_id,
            group_name=group_name,
        )
        group_id = existing_group or self._add_group(
            access_token=access_token,
            campaign_id=campaign_id,
            group_name=group_name,
            region_ids=regions,
        )
        existing_ad = self._find_ad(
            access_token=access_token,
            ad_group_id=group_id,
            href=destination,
        )
        ad_id = existing_ad or self._add_ad(
            access_token=access_token,
            ad_group_id=group_id,
            title=" ".join(str(title or "").split())[:56],
            text=" ".join(str(text or "").split())[:75],
            href=destination,
        )
        return YandexPublicationResult(ad_group_id=str(group_id), ad_id=str(ad_id))

    def _find_group(self, *, access_token: str, campaign_id: int, group_name: str) -> int | None:
        result = self._direct_call(
            service="adgroups",
            token=access_token,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {"CampaignIds": [campaign_id]},
                    "FieldNames": ["Id", "Name", "CampaignId"],
                },
            },
        )
        for item in result.get("AdGroups") or []:
            if str(item.get("Name") or "") == group_name:
                return int(item["Id"])
        return None

    def _add_group(
        self,
        *,
        access_token: str,
        campaign_id: int,
        group_name: str,
        region_ids: list[int],
    ) -> int:
        result = self._direct_call(
            service="adgroups",
            token=access_token,
            payload={
                "method": "add",
                "params": {
                    "AdGroups": [
                        {
                            "Name": group_name,
                            "CampaignId": campaign_id,
                            "RegionIds": region_ids,
                        }
                    ]
                },
            },
        )
        return _first_add_id(result, key="AddResults", error_code="ad_group_creation_failed")

    def _find_ad(self, *, access_token: str, ad_group_id: int, href: str) -> int | None:
        result = self._direct_call(
            service="ads",
            token=access_token,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {"AdGroupIds": [ad_group_id]},
                    "FieldNames": ["Id", "AdGroupId", "Type"],
                    "TextAdFieldNames": ["Href", "Title", "Text"],
                },
            },
        )
        for item in result.get("Ads") or []:
            text_ad = item.get("TextAd") or {}
            if str(text_ad.get("Href") or "") == href:
                return int(item["Id"])
        return None

    def _add_ad(
        self,
        *,
        access_token: str,
        ad_group_id: int,
        title: str,
        text: str,
        href: str,
    ) -> int:
        if not title or not text:
            raise YandexDirectError("ad_copy_empty")
        result = self._direct_call(
            service="ads",
            token=access_token,
            payload={
                "method": "add",
                "params": {
                    "Ads": [
                        {
                            "AdGroupId": ad_group_id,
                            "TextAd": {
                                "Title": title,
                                "Text": text,
                                "Href": href,
                                "Mobile": "NO",
                            },
                        }
                    ]
                },
            },
        )
        return _first_add_id(result, key="AddResults", error_code="ad_creation_failed")

    def _direct_call(
        self,
        *,
        service: str,
        token: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._direct_call_at_root(
            root=self.API_ROOT,
            service=service,
            token=token,
            payload=payload,
        )

    def _managed_direct_call(
        self,
        *,
        service: str,
        token: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._direct_call_at_root(
            root=self.MANAGED_API_ROOT,
            service=service,
            token=token,
            payload=payload,
        )

    def _direct_call_at_root(
        self,
        *,
        root: str,
        service: str,
        token: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        response = self._json_or_error(
            method="POST",
            url=f"{root}/{service}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept-Language": "ru",
                "Content-Type": "application/json; charset=utf-8",
            },
            body=body,
        )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise YandexDirectError("provider_result_missing")
        return result

    def _json_or_error(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
        oauth_call: bool = False,
    ) -> Mapping[str, Any]:
        status, _response_headers, raw = self._transport.request(
            method=method,
            url=url,
            headers=headers,
            body=body,
            timeout=20.0,
        )
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise YandexDirectError(
                "provider_response_invalid",
                retryable=status >= 500,
            ) from exc
        if not isinstance(payload, Mapping):
            raise YandexDirectError("provider_response_invalid", retryable=status >= 500)
        if status >= 400 or "error" in payload or "error_code" in payload:
            code = payload.get("error")
            if isinstance(code, Mapping):
                code = code.get("error_code") or code.get("code")
            code = code or payload.get("error_code") or f"http_{status}"
            retryable = status in {408, 425, 429} or status >= 500
            if not oauth_call and str(code) in {"53", "54", "55", "56"}:
                retryable = True
            raise YandexDirectError(f"provider_{code}", retryable=retryable)
        return payload


def _first_add_id(result: Mapping[str, Any], *, key: str, error_code: str) -> int:
    entries = result.get(key) or []
    if not entries or not isinstance(entries[0], Mapping):
        raise YandexDirectError(error_code)
    first = entries[0]
    if first.get("Errors"):
        errors = first.get("Errors") or []
        code = errors[0].get("Code") if errors and isinstance(errors[0], Mapping) else None
        raise YandexDirectError(f"provider_{code or error_code}")
    identifier = first.get("Id")
    if identifier in (None, ""):
        raise YandexDirectError(error_code)
    return int(identifier)


def _safe_code(value: object) -> str:
    normalized = "_".join(str(value or "provider_error").strip().lower().split())
    filtered = "".join(ch for ch in normalized if ch.isalnum() or ch in "_.-")
    return (filtered or "provider_error")[:120]


__all__ = [
    "JsonHttpTransport",
    "UrllibJsonTransport",
    "YandexAccountIdentity",
    "YandexCampaign",
    "YandexDirectError",
    "YandexDirectProvider",
    "YandexOAuthConfig",
    "YandexPublicationResult",
    "YandexTokenBundle",
]

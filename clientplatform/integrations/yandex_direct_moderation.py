from __future__ import annotations

from typing import Any, Mapping

from clientplatform.domain.ad_connections import normalize_external_campaign_id
from clientplatform.integrations.yandex_direct import (
    JsonHttpTransport,
    YandexAccountIdentity,
    YandexCampaign,
    YandexDirectError,
    YandexDirectProvider,
    YandexOAuthConfig,
    YandexPublicationResult,
)


_SUPPORTED_CAMPAIGN_TYPE = "TEXT_CAMPAIGN"
_SUPPORTED_ADVERTISER_TYPES = {"CLIENT", "SUBCLIENT"}
_PERMISSION_ERROR_CODES = {
    "provider_54": "direct_permission_denied",
    "provider_55": "direct_account_access_denied",
    "provider_56": "direct_account_access_denied",
}


class ModeratingYandexDirectProvider(YandexDirectProvider):
    """Budget-safe Yandex Direct adapter.

    The historical class name is retained for compatibility, but this adapter
    only transfers an idempotent DRAFT into the user's own account. It never
    submits the ad to moderation and never creates, resumes or changes keywords.
    A launch vertical requires a budget snapshot, an explicit cap and a separate
    confirmation contract.
    """

    API_ROOT = "https://api.direct.yandex.com/json/v501"

    def __init__(
        self,
        *,
        oauth: YandexOAuthConfig,
        transport: JsonHttpTransport | None = None,
    ) -> None:
        super().__init__(oauth=oauth, transport=transport)

    def _json_or_error(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
        oauth_call: bool = False,
    ) -> Mapping[str, Any]:
        """Keep permission errors distinct from refreshable token errors."""

        try:
            return super()._json_or_error(
                method=method,
                url=url,
                headers=headers,
                body=body,
                oauth_call=oauth_call,
            )
        except YandexDirectError as exc:
            safe_code = _PERMISSION_ERROR_CODES.get(exc.code)
            if safe_code is None:
                raise
            raise YandexDirectError(safe_code, retryable=False) from exc

    def account_identity(self, *, access_token: str) -> YandexAccountIdentity:
        """Resolve one concrete advertiser from Direct, never from OAuth login."""

        result = self._direct_call(
            service="clients",
            token=access_token,
            payload={
                "method": "get",
                "params": {
                    "FieldNames": [
                        "ClientId",
                        "ClientInfo",
                        "Login",
                        "Type",
                        "Archived",
                        "Grants",
                    ]
                },
            },
        )
        clients = result.get("Clients") or []
        if len(clients) != 1 or not isinstance(clients[0], Mapping):
            raise YandexDirectError("direct_account_identity_ambiguous")
        client = clients[0]
        if str(client.get("Archived") or "NO").strip().upper() == "YES":
            raise YandexDirectError("direct_account_archived")
        account_type = str(client.get("Type") or "").strip().upper()
        if account_type == "AGENCY":
            raise YandexDirectError("direct_agency_account_ambiguous")
        if account_type not in _SUPPORTED_ADVERTISER_TYPES:
            raise YandexDirectError("direct_account_type_unsupported")
        client_id = normalize_external_campaign_id(client.get("ClientId"))
        login = " ".join(str(client.get("Login") or "").split())
        if not login:
            raise YandexDirectError("direct_account_login_missing")
        grants = client.get("Grants") or []
        privileges = {
            str(item.get("Privilege") or "").strip().upper()
            for item in grants
            if isinstance(item, Mapping)
            and str(item.get("Value") or "").strip().upper() == "YES"
        }
        if "EDIT_CAMPAIGNS" not in privileges:
            raise YandexDirectError("direct_account_is_read_only")
        return YandexAccountIdentity(account_id=client_id, login=login)

    def list_text_campaigns(self, *, access_token: str) -> list[YandexCampaign]:
        """Return only active, accepted and payment-ready text campaigns."""

        result = self._direct_call(
            service="campaigns",
            token=access_token,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {
                        "Types": [_SUPPORTED_CAMPAIGN_TYPE],
                        "States": ["ON"],
                        "Statuses": ["ACCEPTED"],
                        "StatusesPayment": ["ALLOWED"],
                    },
                    "FieldNames": [
                        "Id",
                        "Name",
                        "State",
                        "Status",
                        "StatusPayment",
                        "Type",
                    ],
                },
            },
        )
        campaigns: list[YandexCampaign] = []
        for item in result.get("Campaigns") or []:
            if not isinstance(item, Mapping):
                continue
            campaign_type = str(item.get("Type") or "").strip().upper()
            state = str(item.get("State") or "UNKNOWN").strip().upper()
            status = str(item.get("Status") or "UNKNOWN").strip().upper()
            status_payment = str(
                item.get("StatusPayment") or "UNKNOWN"
            ).strip().upper()
            if (
                campaign_type != _SUPPORTED_CAMPAIGN_TYPE
                or state != "ON"
                or status != "ACCEPTED"
                or status_payment != "ALLOWED"
            ):
                continue
            campaigns.append(
                YandexCampaign(
                    campaign_id=normalize_external_campaign_id(item.get("Id")),
                    name=" ".join(
                        str(item.get("Name") or "Без названия").split()
                    )[:255],
                    state=state,
                    status=status,
                    campaign_type=campaign_type,
                )
            )
        return campaigns

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
        self._assert_safe_campaign(
            access_token=access_token,
            campaign_id=campaign_id,
        )
        result = super().publish_text_ad(
            access_token=access_token,
            external_campaign_id=external_campaign_id,
            region_ids=region_ids,
            title=title,
            text=text,
            href=href,
            idempotency_key=idempotency_key,
        )
        status = self._ad_status(
            access_token=access_token,
            ad_id=int(result.ad_id),
        )
        if status != "DRAFT":
            raise YandexDirectError("existing_ad_is_not_draft")
        return result

    def _assert_safe_campaign(self, *, access_token: str, campaign_id: int) -> None:
        """Revalidate every safety-relevant campaign property at write time."""

        result = self._direct_call(
            service="campaigns",
            token=access_token,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {"Ids": [campaign_id]},
                    "FieldNames": [
                        "Id",
                        "State",
                        "Status",
                        "StatusPayment",
                        "Type",
                    ],
                },
            },
        )
        campaigns = result.get("Campaigns") or []
        if not campaigns or not isinstance(campaigns[0], Mapping):
            raise YandexDirectError("campaign_not_found")
        item = campaigns[0]
        if int(item.get("Id") or 0) != campaign_id:
            raise YandexDirectError("campaign_identity_mismatch")
        if str(item.get("State") or "").strip().upper() != "ON":
            raise YandexDirectError("campaign_not_active")
        if str(item.get("Status") or "").strip().upper() != "ACCEPTED":
            raise YandexDirectError("campaign_not_accepted")
        if str(item.get("StatusPayment") or "").strip().upper() != "ALLOWED":
            raise YandexDirectError("campaign_payment_not_allowed")
        if str(item.get("Type") or "").strip().upper() != _SUPPORTED_CAMPAIGN_TYPE:
            raise YandexDirectError("campaign_type_unsupported")

    def _ad_status(self, *, access_token: str, ad_id: int) -> str:
        result = self._direct_call(
            service="ads",
            token=access_token,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {"Ids": [ad_id]},
                    "FieldNames": ["Id", "Status"],
                },
            },
        )
        ads = result.get("Ads") or []
        if not ads or not isinstance(ads[0], Mapping):
            raise YandexDirectError("ad_status_missing")
        item: Mapping[str, Any] = ads[0]
        if int(item.get("Id") or 0) != ad_id:
            raise YandexDirectError("ad_status_mismatch")
        return str(item.get("Status") or "UNKNOWN").strip().upper()


__all__ = ["ModeratingYandexDirectProvider"]
from __future__ import annotations

import re
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


class ModeratingYandexDirectProvider(YandexDirectProvider):
    """Budget-safe Yandex Direct adapter.

    The historical class name is retained for compatibility, but this adapter
    deliberately does *not* submit ads to moderation. It transfers an
    idempotent DRAFT into the user's own account. Turning that draft into a
    spending ad requires a separate budget-snapshot and explicit launch
    contract, which is intentionally outside this first provider slice.

    This fail-closed behaviour prevents a generated keyword from consuming the
    budget of an existing campaign merely because a Telegram callback was
    confirmed.
    """

    API_ROOT = "https://api.direct.yandex.com/json/v501"

    def __init__(
        self,
        *,
        oauth: YandexOAuthConfig,
        transport: JsonHttpTransport | None = None,
    ) -> None:
        super().__init__(oauth=oauth, transport=transport)

    def account_identity(self, *, access_token: str) -> YandexAccountIdentity:
        """Resolve and authorize the connected *Direct* account.

        A generic Yandex profile is not an advertising account. Clients.get is
        the authoritative source for ClientId, representative Login, archive
        state and campaign-edit grants. Read-only or ambiguous connections are
        rejected before any OAuth material is activated in ClientPlatform.
        """

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
        client_id = normalize_external_campaign_id(client.get("ClientId"))
        login = " ".join(str(client.get("Login") or "").split())
        if not login:
            raise YandexDirectError("direct_account_login_missing")
        grants = client.get("Grants") or []
        privileges = {
            str(item.get("Privilege") or "").strip().upper()
            for item in grants
            if isinstance(item, Mapping)
        }
        if "EDIT_CAMPAIGNS" not in privileges:
            raise YandexDirectError("direct_account_is_read_only")
        return YandexAccountIdentity(account_id=client_id, login=login)

    def list_text_campaigns(self, *, access_token: str) -> list[YandexCampaign]:
        """Return only active, accepted legacy text campaigns.

        Unified campaigns have materially different targeting and budget
        semantics. They stay unsupported until their budget and placement
        contracts are represented explicitly in ClientPlatform.
        """

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
                    },
                    "FieldNames": ["Id", "Name", "State", "Status", "Type"],
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
            if (
                campaign_type != _SUPPORTED_CAMPAIGN_TYPE
                or state != "ON"
                or status != "ACCEPTED"
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
        self._ensure_exact_keyword(
            access_token=access_token,
            ad_group_id=int(result.ad_group_id),
            title=title,
        )
        status = self._ad_status(
            access_token=access_token,
            ad_id=int(result.ad_id),
        )
        if status != "DRAFT":
            raise YandexDirectError("existing_ad_is_not_draft")
        return result

    def _assert_safe_campaign(self, *, access_token: str, campaign_id: int) -> None:
        result = self._direct_call(
            service="campaigns",
            token=access_token,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {"Ids": [campaign_id]},
                    "FieldNames": ["Id", "State", "Status", "Type"],
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
        if str(item.get("Type") or "").strip().upper() != _SUPPORTED_CAMPAIGN_TYPE:
            raise YandexDirectError("campaign_type_unsupported")

    def _ensure_exact_keyword(
        self,
        *,
        access_token: str,
        ad_group_id: int,
        title: str,
    ) -> int:
        phrase = _exact_keyword_phrase(title)
        result = self._direct_call(
            service="keywords",
            token=access_token,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {"AdGroupIds": [ad_group_id]},
                    "FieldNames": ["Id", "Keyword", "State", "Status"],
                },
            },
        )
        for item in result.get("Keywords") or []:
            if not isinstance(item, Mapping):
                continue
            observed = " ".join(str(item.get("Keyword") or "").split())
            if observed != phrase:
                continue
            keyword_id = int(item.get("Id") or 0)
            if keyword_id <= 0:
                raise YandexDirectError("keyword_identity_invalid")
            return keyword_id

        created = self._direct_call(
            service="keywords",
            token=access_token,
            payload={
                "method": "add",
                "params": {
                    "Keywords": [
                        {
                            "AdGroupId": ad_group_id,
                            "Keyword": phrase,
                        }
                    ]
                },
            },
        )
        return _first_action_id(
            created,
            key="AddResults",
            fallback_code="keyword_creation_failed",
        )

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


def _exact_keyword_phrase(title: str) -> str:
    cleaned = re.sub(r"[^A-Za-zА-Яа-яЁё\s-]", " ", str(title or ""))
    words: list[str] = []
    for raw in cleaned.split():
        word = raw.strip("-_")[:22]
        if not word:
            continue
        words.append("!" + word)
        if len(words) == 7:
            break
    if not words:
        raise YandexDirectError("keyword_phrase_empty")
    # Quotes fix the number of words; ! fixes word forms. The draft remains
    # unmoderated, so even this narrow criterion cannot spend money yet.
    return '"' + " ".join(words) + '"'


def _first_action_id(
    result: Mapping[str, Any],
    *,
    key: str,
    fallback_code: str,
) -> int:
    entries = result.get(key) or []
    if not entries or not isinstance(entries[0], Mapping):
        raise YandexDirectError(fallback_code)
    first = entries[0]
    if first.get("Errors"):
        errors = first.get("Errors") or []
        code = (
            errors[0].get("Code")
            if errors and isinstance(errors[0], Mapping)
            else None
        )
        raise YandexDirectError(f"provider_{code or fallback_code}")
    identifier = first.get("Id")
    if identifier in (None, ""):
        raise YandexDirectError(fallback_code)
    return int(identifier)


__all__ = ["ModeratingYandexDirectProvider"]

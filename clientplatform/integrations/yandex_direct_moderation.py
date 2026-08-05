from __future__ import annotations

import re
from typing import Any, Mapping

from clientplatform.domain.ad_connections import (
    normalize_external_campaign_id,
    normalize_region_ids,
)
from clientplatform.integrations.yandex_direct import (
    JsonHttpTransport,
    YandexCampaign,
    YandexDirectError,
    YandexDirectProvider,
    YandexOAuthConfig,
    YandexPublicationResult,
)


_READY_OR_REVIEWING_STATUSES = {
    "MODERATION",
    "PREACCEPTED",
    "ACCEPTED",
}
_SUPPORTED_CAMPAIGN_TYPES = {
    "TEXT_CAMPAIGN",
    "UNIFIED_CAMPAIGN",
}


class ModeratingYandexDirectProvider(YandexDirectProvider):
    """Current Yandex Direct adapter for legacy and unified campaigns.

    OAuth and the safe HTTP boundary remain in the base adapter. This extension
    uses API v501, supports both legacy text campaigns and current unified
    performance campaigns, reconciles remote objects before creation and moves
    only DRAFT ads into moderation.
    """

    API_ROOT = "https://api.direct.yandex.com/json/v501"

    def __init__(
        self,
        *,
        oauth: YandexOAuthConfig,
        transport: JsonHttpTransport | None = None,
    ) -> None:
        super().__init__(oauth=oauth, transport=transport)

    def list_text_campaigns(self, *, access_token: str) -> list[YandexCampaign]:
        """Return active reviewed campaigns that can immediately serve ads."""

        result = self._direct_call(
            service="campaigns",
            token=access_token,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {
                        "Types": sorted(_SUPPORTED_CAMPAIGN_TYPES),
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
                campaign_type not in _SUPPORTED_CAMPAIGN_TYPES
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
        campaign_type = self._campaign_type(
            access_token=access_token,
            campaign_id=campaign_id,
        )
        if campaign_type == "TEXT_CAMPAIGN":
            result = super().publish_text_ad(
                access_token=access_token,
                external_campaign_id=external_campaign_id,
                region_ids=region_ids,
                title=title,
                text=text,
                href=href,
                idempotency_key=idempotency_key,
            )
        elif campaign_type == "UNIFIED_CAMPAIGN":
            result = self._publish_responsive_ad(
                access_token=access_token,
                campaign_id=campaign_id,
                region_ids=region_ids,
                title=title,
                text=text,
                href=href,
                idempotency_key=idempotency_key,
            )
        else:
            raise YandexDirectError("campaign_type_unsupported")

        status = self._ad_status(
            access_token=access_token,
            ad_id=int(result.ad_id),
        )
        if status == "DRAFT":
            self._moderate_ad(
                access_token=access_token,
                ad_id=int(result.ad_id),
            )
        elif status in _READY_OR_REVIEWING_STATUSES:
            pass
        elif status == "REJECTED":
            raise YandexDirectError("ad_rejected_requires_manual_review")
        else:
            raise YandexDirectError("ad_moderation_status_unknown")
        return result

    def _campaign_type(self, *, access_token: str, campaign_id: int) -> str:
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
        state = str(item.get("State") or "UNKNOWN").strip().upper()
        status = str(item.get("Status") or "UNKNOWN").strip().upper()
        if state != "ON":
            raise YandexDirectError("campaign_not_active")
        if status != "ACCEPTED":
            raise YandexDirectError("campaign_not_accepted")
        campaign_type = str(item.get("Type") or "UNKNOWN").strip().upper()
        if campaign_type not in _SUPPORTED_CAMPAIGN_TYPES:
            raise YandexDirectError("campaign_type_unsupported")
        return campaign_type

    def _publish_responsive_ad(
        self,
        *,
        access_token: str,
        campaign_id: int,
        region_ids: tuple[int, ...],
        title: str,
        text: str,
        href: str,
        idempotency_key: str,
    ) -> YandexPublicationResult:
        regions = list(normalize_region_ids(region_ids))
        destination = str(href or "").strip()
        if not destination.startswith("https://"):
            raise YandexDirectError("destination_url_invalid")
        normalized_title = " ".join(str(title or "").split())[:56]
        normalized_text = " ".join(str(text or "").split())[:75]
        if not normalized_title or not normalized_text:
            raise YandexDirectError("ad_copy_empty")

        group_name = f"ClientPlatform {idempotency_key}"[:255]
        existing_group = self._find_group(
            access_token=access_token,
            campaign_id=campaign_id,
            group_name=group_name,
        )
        group_id = existing_group or self._add_unified_group(
            access_token=access_token,
            campaign_id=campaign_id,
            group_name=group_name,
            region_ids=regions,
        )
        existing_ad = self._find_responsive_ad(
            access_token=access_token,
            ad_group_id=group_id,
            href=destination,
        )
        ad_id = existing_ad or self._add_responsive_ad(
            access_token=access_token,
            ad_group_id=group_id,
            title=normalized_title,
            text=normalized_text,
            href=destination,
        )
        self._ensure_keyword(
            access_token=access_token,
            ad_group_id=group_id,
            title=normalized_title,
        )
        return YandexPublicationResult(
            ad_group_id=str(group_id),
            ad_id=str(ad_id),
        )

    def _add_unified_group(
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
                            "UnifiedAdGroup": {"OfferRetargeting": "NO"},
                        }
                    ]
                },
            },
        )
        return _first_action_id(
            result,
            key="AddResults",
            fallback_code="unified_ad_group_creation_failed",
        )

    def _find_responsive_ad(
        self,
        *,
        access_token: str,
        ad_group_id: int,
        href: str,
    ) -> int | None:
        result = self._direct_call(
            service="ads",
            token=access_token,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {"AdGroupIds": [ad_group_id]},
                    "FieldNames": ["Id", "AdGroupId", "Type"],
                    "ResponsiveAdFieldNames": ["Href", "Titles", "Texts"],
                },
            },
        )
        for item in result.get("Ads") or []:
            if not isinstance(item, Mapping):
                continue
            responsive = item.get("ResponsiveAd") or {}
            if isinstance(responsive, Mapping) and str(
                responsive.get("Href") or ""
            ) == href:
                return int(item["Id"])
        return None

    def _add_responsive_ad(
        self,
        *,
        access_token: str,
        ad_group_id: int,
        title: str,
        text: str,
        href: str,
    ) -> int:
        result = self._direct_call(
            service="ads",
            token=access_token,
            payload={
                "method": "add",
                "params": {
                    "Ads": [
                        {
                            "AdGroupId": ad_group_id,
                            "ResponsiveAd": {
                                "Titles": [title],
                                "Texts": [text],
                                "Href": href,
                            },
                        }
                    ]
                },
            },
        )
        return _first_action_id(
            result,
            key="AddResults",
            fallback_code="responsive_ad_creation_failed",
        )

    def _ensure_keyword(
        self,
        *,
        access_token: str,
        ad_group_id: int,
        title: str,
    ) -> int:
        phrase = _keyword_phrase(title)
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
            observed = " ".join(
                str(item.get("Keyword") or "").lower().split()
            )
            if observed != phrase.lower():
                continue
            keyword_id = int(item.get("Id") or 0)
            if keyword_id <= 0:
                raise YandexDirectError("keyword_identity_invalid")
            if str(item.get("State") or "").strip().upper() == "SUSPENDED":
                self._resume_keyword(
                    access_token=access_token,
                    keyword_id=keyword_id,
                )
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

    def _resume_keyword(self, *, access_token: str, keyword_id: int) -> None:
        result = self._direct_call(
            service="keywords",
            token=access_token,
            payload={
                "method": "resume",
                "params": {"SelectionCriteria": {"Ids": [keyword_id]}},
            },
        )
        resumed_id = _first_action_id(
            result,
            key="ResumeResults",
            fallback_code="keyword_resume_failed",
        )
        if resumed_id != keyword_id:
            raise YandexDirectError("keyword_resume_result_mismatch")

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

    def _moderate_ad(self, *, access_token: str, ad_id: int) -> None:
        result = self._direct_call(
            service="ads",
            token=access_token,
            payload={
                "method": "moderate",
                "params": {
                    "SelectionCriteria": {"Ids": [ad_id]},
                },
            },
        )
        entries = result.get("ModerateResults") or []
        if not entries or not isinstance(entries[0], Mapping):
            raise YandexDirectError("ad_moderation_result_missing")
        first = entries[0]
        if first.get("Errors"):
            errors = first.get("Errors") or []
            code = (
                errors[0].get("Code")
                if errors and isinstance(errors[0], Mapping)
                else None
            )
            raise YandexDirectError(f"provider_{code or 'ad_moderation_failed'}")
        if int(first.get("Id") or 0) != ad_id:
            raise YandexDirectError("ad_moderation_result_mismatch")


def _keyword_phrase(title: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", " ", str(title or ""), flags=re.UNICODE)
    words: list[str] = []
    for raw in cleaned.split():
        word = raw.strip("-_")[:35]
        if not word:
            continue
        words.append(word)
        if len(words) == 7:
            break
    phrase = " ".join(words)
    if not phrase:
        raise YandexDirectError("keyword_phrase_empty")
    return phrase


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

from __future__ import annotations

from typing import Any, Mapping

from clientplatform.integrations.yandex_direct import (
    JsonHttpTransport,
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


class ModeratingYandexDirectProvider(YandexDirectProvider):
    """Yandex Direct adapter that finishes the publish path with Ads.moderate.

    The base adapter owns OAuth, reconciliation and remote object creation. This
    narrow extension adds the final provider transition required for a created
    DRAFT ad to enter Yandex review. Replays remain safe: ads that are already
    being reviewed or have been accepted are not submitted again.
    """

    def __init__(
        self,
        *,
        oauth: YandexOAuthConfig,
        transport: JsonHttpTransport | None = None,
    ) -> None:
        super().__init__(oauth=oauth, transport=transport)

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


__all__ = ["ModeratingYandexDirectProvider"]

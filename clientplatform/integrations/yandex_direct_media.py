from __future__ import annotations

import base64
from typing import Any, Mapping

from clientplatform.integrations.yandex_direct import YandexDirectError, YandexOAuthConfig
from clientplatform.integrations.yandex_direct_moderation import (
    ModeratingYandexDirectProvider,
)


def _first_action_value(
    result: Mapping[str, Any],
    *,
    key: str,
    value_key: str,
    error_code: str,
) -> str:
    entries = result.get(key) or []
    if not entries or not isinstance(entries[0], Mapping):
        raise YandexDirectError(error_code)
    first = entries[0]
    errors = first.get("Errors") or []
    if errors:
        code = errors[0].get("Code") if isinstance(errors[0], Mapping) else None
        raise YandexDirectError(f"provider_{code or error_code}")
    value = first.get(value_key)
    if value in (None, ""):
        raise YandexDirectError(error_code)
    return str(value)


def _assert_update_ok(result: Mapping[str, Any], *, error_code: str) -> None:
    entries = result.get("UpdateResults") or []
    if not entries or not isinstance(entries[0], Mapping):
        raise YandexDirectError(error_code)
    errors = entries[0].get("Errors") or []
    if errors:
        code = errors[0].get("Code") if isinstance(errors[0], Mapping) else None
        raise YandexDirectError(f"provider_{code or error_code}")


class MediaAwareYandexDirectProvider(ModeratingYandexDirectProvider):
    """Attach user/generated media to a DRAFT without initiating spend."""

    def upload_image(
        self,
        *,
        access_token: str,
        payload: bytes,
        name: str,
    ) -> str:
        if not payload or len(payload) > 10_000_000:
            raise YandexDirectError("ad_image_size_invalid")
        result = self._direct_call(
            service="adimages",
            token=access_token,
            payload={
                "method": "add",
                "params": {
                    "AdImages": [
                        {
                            "ImageData": base64.b64encode(payload).decode("ascii"),
                            "Type": "AUTO",
                            "Name": " ".join(str(name or "ClientPlatform image").split())[:255],
                        }
                    ]
                },
            },
        )
        return _first_action_value(
            result,
            key="AddResults",
            value_key="AdImageHash",
            error_code="ad_image_upload_failed",
        )

    def attach_image(
        self,
        *,
        access_token: str,
        ad_id: str,
        image_hash: str,
    ) -> None:
        result = self._direct_call(
            service="ads",
            token=access_token,
            payload={
                "method": "update",
                "params": {
                    "Ads": [
                        {
                            "Id": int(ad_id),
                            "TextAd": {"AdImageHash": str(image_hash)},
                        }
                    ]
                },
            },
        )
        _assert_update_ok(result, error_code="ad_image_attachment_failed")

    def upload_video(
        self,
        *,
        access_token: str,
        payload: bytes,
        name: str,
    ) -> str:
        if not payload or len(payload) > 100_000_000:
            raise YandexDirectError("ad_video_size_invalid")
        result = self._direct_call(
            service="advideos",
            token=access_token,
            payload={
                "method": "add",
                "params": {
                    "AdVideos": [
                        {
                            "VideoData": base64.b64encode(payload).decode("ascii"),
                            "Name": " ".join(str(name or "ClientPlatform video").split())[:255],
                        }
                    ]
                },
            },
        )
        return _first_action_value(
            result,
            key="AddResults",
            value_key="Id",
            error_code="ad_video_upload_failed",
        )

    def video_status(self, *, access_token: str, video_id: str) -> str:
        result = self._direct_call(
            service="advideos",
            token=access_token,
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {"Ids": [str(video_id)]},
                    "FieldNames": ["Id", "Status"],
                    "Page": {"Limit": 10, "Offset": 0},
                },
            },
        )
        items = result.get("AdVideos") or []
        if not items or not isinstance(items[0], Mapping):
            raise YandexDirectError("ad_video_status_missing")
        item = items[0]
        if str(item.get("Id") or "") != str(video_id):
            raise YandexDirectError("ad_video_status_mismatch")
        status = str(item.get("Status") or "UNKNOWN").strip().upper()
        if status not in {"READY", "ERROR", "CONVERTING", "NEW"}:
            raise YandexDirectError("ad_video_status_invalid")
        return status

    def create_video_extension(
        self,
        *,
        access_token: str,
        video_id: str,
    ) -> str:
        result = self._direct_call(
            service="creatives",
            token=access_token,
            payload={
                "method": "add",
                "params": {
                    "Creatives": [
                        {"VideoExtensionCreative": {"VideoId": str(video_id)}}
                    ]
                },
            },
        )
        return _first_action_value(
            result,
            key="AddResults",
            value_key="Id",
            error_code="ad_video_creative_failed",
        )

    def attach_video(
        self,
        *,
        access_token: str,
        ad_id: str,
        creative_id: str,
    ) -> None:
        result = self._direct_call(
            service="ads",
            token=access_token,
            payload={
                "method": "update",
                "params": {
                    "Ads": [
                        {
                            "Id": int(ad_id),
                            "TextAd": {
                                "VideoExtension": {"CreativeId": int(creative_id)}
                            },
                        }
                    ]
                },
            },
        )
        _assert_update_ok(result, error_code="ad_video_attachment_failed")


__all__ = ["MediaAwareYandexDirectProvider"]

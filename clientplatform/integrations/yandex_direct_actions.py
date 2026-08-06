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


def _token(value: object, name: str) -> str:
    token = str(value or "").strip().upper()
    if not token or len(token) > 160 or "\x00" in token:
        raise YandexDirectError(f"{name}_invalid")
    return token


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


class YandexDirectAdActions(YandexDirectProvider):
    """Exact-ID operations only; never resumes or broadens advertising."""

    def __init__(
        self,
        *,
        oauth: YandexOAuthConfig,
        transport: JsonHttpTransport | None = None,
    ) -> None:
        super().__init__(oauth=oauth, transport=transport)

    def _call(
        self,
        *,
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
            url=f"{self.API_ROOT}/ads",
            headers=headers,
            body=_canonical_json(payload).encode("utf-8"),
        )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise YandexDirectError("provider_result_missing")
        return result

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


__all__ = ["YandexAdActionResult", "YandexAdState", "YandexDirectAdActions"]

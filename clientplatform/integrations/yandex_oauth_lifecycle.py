from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlencode

from clientplatform.integrations.yandex_direct import (
    JsonHttpTransport,
    UrllibJsonTransport,
    YandexDirectError,
)


@dataclass(frozen=True, slots=True)
class YandexRevocationResult:
    provider_revoked: bool
    local_erasure_allowed: bool


class YandexOAuthLifecycle:
    REVOKE_URL = "https://oauth.yandex.com/revoke_token"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        transport: JsonHttpTransport | None = None,
    ) -> None:
        self._client_id = str(client_id or "").strip()
        self._client_secret = str(client_secret or "").strip()
        self._transport = transport or UrllibJsonTransport()

    def revoke(self, *, access_token: str) -> YandexRevocationResult:
        token = str(access_token or "").strip()
        if not token:
            return YandexRevocationResult(
                provider_revoked=False,
                local_erasure_allowed=True,
            )
        if not self._client_id or not self._client_secret:
            return YandexRevocationResult(
                provider_revoked=False,
                local_erasure_allowed=True,
            )
        body = urlencode(
            {
                "access_token": token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
        ).encode("ascii")
        status, _headers, raw = self._transport.request(
            method="POST",
            url=self.REVOKE_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body,
            timeout=20.0,
        )
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise YandexDirectError(
                "oauth_revoke_response_invalid",
                retryable=status >= 500,
            ) from exc
        if status == 200 and payload.get("status") == "ok":
            return YandexRevocationResult(
                provider_revoked=True,
                local_erasure_allowed=True,
            )
        error = str(payload.get("error") or f"http_{status}").strip().lower()
        if error == "unsupported_token_type":
            return YandexRevocationResult(
                provider_revoked=False,
                local_erasure_allowed=True,
            )
        raise YandexDirectError(
            f"oauth_revoke_{error}",
            retryable=status in {408, 425, 429} or status >= 500,
        )


__all__ = ["YandexOAuthLifecycle", "YandexRevocationResult"]

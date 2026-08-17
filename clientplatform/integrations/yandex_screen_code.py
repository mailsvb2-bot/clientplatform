from __future__ import annotations

import os
from urllib.parse import urlencode

from clientplatform.integrations.yandex_direct import (
    JsonHttpTransport,
    YandexDirectError,
    YandexOAuthConfig,
    YandexTokenBundle,
)
from clientplatform.integrations.yandex_direct_moderation import (
    ModeratingYandexDirectProvider,
)


YANDEX_SCREEN_CODE_REDIRECT_URI = "https://oauth.yandex.ru/verification_code"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_MAX_CONFIRMATION_CODE_LENGTH = 1024


def normalize_yandex_confirmation_code(value: str | None) -> str:
    """Return the bounded opaque confirmation code rendered by Yandex OAuth.

    Yandex's screen-code contract defines the value as a confirmation code that
    must be sent back to the OAuth token endpoint. It does not define a client-
    side alphabet for that value, so ClientPlatform must not reject a code just
    because its copied representation contains non-ASCII or other characters we
    did not anticipate. The OAuth provider remains the authority for code
    validity. We only reject empty or unreasonably large input locally.

    Leading/trailing user whitespace is ignored. A leading ``# `` remains
    tolerated for compatibility with users who copy a fragment-style prefix
    together with the displayed code.
    """

    raw = str(value or "")
    if len(raw) > _MAX_CONFIRMATION_CODE_LENGTH:
        raise YandexDirectError("oauth_code_invalid")
    code = raw.strip()
    if code.startswith("# "):
        code = code[2:].strip()
    if not code or len(code) > _MAX_CONFIRMATION_CODE_LENGTH:
        raise YandexDirectError("oauth_code_invalid")
    return code


def screen_code_configuration_available() -> bool:
    """Return whether the owner-facing screen-code connection can actually start."""

    connections_enabled = str(
        os.getenv("CLIENTPLATFORM_AD_CONNECTIONS_ENABLED") or ""
    ).strip().lower() in _TRUE_VALUES
    client_id = str(
        os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID") or ""
    ).strip()
    client_secret = str(
        os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET") or ""
    ).strip()
    redirect_uri = str(
        os.getenv("CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI") or ""
    ).strip()
    return bool(
        connections_enabled
        and client_id
        and client_secret
        and redirect_uri == YANDEX_SCREEN_CODE_REDIRECT_URI
    )


class YandexScreenCodeDirectProvider(ModeratingYandexDirectProvider):
    """Yandex Direct adapter for the immutable browser-displayed code flow."""

    AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
    TOKEN_URL = "https://oauth.yandex.ru/token"

    def __init__(
        self,
        *,
        oauth: YandexOAuthConfig,
        transport: JsonHttpTransport | None = None,
    ) -> None:
        if oauth.redirect_uri != YANDEX_SCREEN_CODE_REDIRECT_URI:
            raise ValueError("Yandex screen-code redirect URI is invalid")
        super().__init__(oauth=oauth, transport=transport)

    def exchange_code(self, *, code: str, verifier: str) -> YandexTokenBundle:
        confirmation_code = normalize_yandex_confirmation_code(code)
        fields = {
            "grant_type": "authorization_code",
            "code": confirmation_code,
            "client_id": self._oauth.client_id,
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
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise YandexDirectError("oauth_access_token_missing")
        scope_raw = payload.get("scope") or []
        if isinstance(scope_raw, str):
            scope = tuple(item for item in scope_raw.replace(",", " ").split() if item)
        else:
            scope = tuple(str(item) for item in scope_raw if str(item).strip())
        expires_raw = payload.get("expires_in")
        return YandexTokenBundle(
            access_token=access_token,
            token_type=str(payload.get("token_type") or "bearer"),
            expires_in=None if expires_raw in (None, "") else int(expires_raw),
            refresh_token=str(payload.get("refresh_token") or "").strip() or None,
            scope=scope,
        )


def screen_code_provider_from_environment() -> YandexScreenCodeDirectProvider:
    connections_enabled = str(
        os.getenv("CLIENTPLATFORM_AD_CONNECTIONS_ENABLED") or ""
    ).strip().lower() in _TRUE_VALUES
    client_id = str(
        os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID") or ""
    ).strip()
    client_secret = str(
        os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET") or ""
    ).strip()
    redirect_uri = str(
        os.getenv("CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI") or ""
    ).strip()
    if not connections_enabled:
        raise RuntimeError("advertising account connections are disabled")
    if not client_id:
        raise RuntimeError("Yandex Direct OAuth application is not configured")
    if not client_secret:
        raise RuntimeError("Yandex Direct OAuth secret is not configured")
    if redirect_uri != YANDEX_SCREEN_CODE_REDIRECT_URI:
        raise RuntimeError("Yandex Direct screen-code redirect is not configured")
    return YandexScreenCodeDirectProvider(
        oauth=YandexOAuthConfig(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
    )


__all__ = [
    "YANDEX_SCREEN_CODE_REDIRECT_URI",
    "YandexScreenCodeDirectProvider",
    "normalize_yandex_confirmation_code",
    "screen_code_configuration_available",
    "screen_code_provider_from_environment",
]

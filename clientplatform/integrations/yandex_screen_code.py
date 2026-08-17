from __future__ import annotations

import os
from typing import Mapping
from urllib.parse import urlencode

from clientplatform.domain.ad_connections import pkce_challenge
from clientplatform.integrations.yandex_direct import (
    JsonHttpTransport,
    YandexAccountIdentity,
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
_MAX_LOGIN_HINT_LENGTH = 320


def normalize_yandex_confirmation_code(value: str | None) -> str:
    """Return the bounded opaque confirmation code rendered by Yandex OAuth.

    The confirmation code belongs to the OAuth provider. ClientPlatform must not
    guess its alphabet, Unicode form, whitespace rules, or any other internal
    syntax before the provider sees it. We only remove presentation envelope that
    can be introduced around a copied Telegram message: outer whitespace and the
    optional leading ``# `` marker already supported by the owner flow. The raw
    message is bounded before trimming so an oversized payload cannot be
    sanitized into acceptance. Yandex OAuth remains the authority for whether the
    resulting code is valid, expired, malformed, or already used.
    """

    if not isinstance(value, str) or len(value) > _MAX_CONFIRMATION_CODE_LENGTH:
        raise YandexDirectError("oauth_code_invalid")
    code = value.strip()
    if code.startswith("# "):
        code = code[2:].strip()
    if not code or len(code) > _MAX_CONFIRMATION_CODE_LENGTH:
        raise YandexDirectError("oauth_code_invalid")
    return code


def normalize_yandex_login_hint(value: str | None) -> str:
    """Normalize a user-selected Yandex login/email used only as an OAuth hint."""

    if not isinstance(value, str) or len(value) > _MAX_LOGIN_HINT_LENGTH:
        raise YandexDirectError("oauth_login_hint_invalid")
    hint = value.strip()
    if (
        not hint
        or len(hint) > _MAX_LOGIN_HINT_LENGTH
        or any(character.isspace() for character in hint)
    ):
        raise YandexDirectError("oauth_login_hint_invalid")
    return hint


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
        login_hint: str | None = None,
    ) -> None:
        if oauth.redirect_uri != YANDEX_SCREEN_CODE_REDIRECT_URI:
            raise ValueError("Yandex screen-code redirect URI is invalid")
        super().__init__(oauth=oauth, transport=transport)
        self._login_hint = (
            None if login_hint is None else normalize_yandex_login_hint(login_hint)
        )

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
        if self._login_hint:
            params["login_hint"] = self._login_hint
        return self.AUTHORIZE_URL + "?" + urlencode(params)

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

    def account_identity(self, *, access_token: str) -> YandexAccountIdentity:
        """Prove the connected advertising identity with Direct, not generic Yandex ID."""

        try:
            result = self._direct_call(
                service="clients",
                token=access_token,
                payload={
                    "method": "get",
                    "params": {"FieldNames": ["ClientId", "Login"]},
                },
            )
        except YandexDirectError as exc:
            raise YandexDirectError(
                f"direct_identity_{exc.code}",
                retryable=exc.retryable,
            ) from exc

        clients = result.get("Clients")
        if (
            not isinstance(clients, list)
            or len(clients) != 1
            or not isinstance(clients[0], Mapping)
        ):
            raise YandexDirectError("direct_identity_response_invalid")
        item = clients[0]
        account_id = str(item.get("ClientId") or "").strip()
        login = str(item.get("Login") or "").strip()
        if not account_id or not login:
            raise YandexDirectError("direct_identity_missing")
        return YandexAccountIdentity(account_id=account_id, login=login)


def screen_code_provider_from_environment(
    *,
    login_hint: str | None = None,
) -> YandexScreenCodeDirectProvider:
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
        ),
        login_hint=login_hint,
    )


__all__ = [
    "YANDEX_SCREEN_CODE_REDIRECT_URI",
    "YandexScreenCodeDirectProvider",
    "normalize_yandex_confirmation_code",
    "normalize_yandex_login_hint",
    "screen_code_configuration_available",
    "screen_code_provider_from_environment",
]

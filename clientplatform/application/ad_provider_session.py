from __future__ import annotations

import os
from typing import Callable, TypeVar

from clientplatform.domain.ad_connections import AdConnection, AdPublicationJob
from clientplatform.infrastructure.ad_credential_vault import (
    AdCredentialVault,
    AgeAdCredentialVault,
)
from clientplatform.infrastructure.ad_worker_store import AdWorkerStore
from clientplatform.integrations.yandex_direct import (
    YandexDirectError,
    YandexOAuthConfig,
    YandexTokenBundle,
)
from clientplatform.integrations.yandex_direct_media import MediaAwareYandexDirectProvider
from services.db import get_db, get_db_ro


_AUTH_ERRORS = frozenset(
    {
        "provider_http_401",
        "provider_53",
        "provider_54",
        "provider_55",
        "provider_56",
        "provider_invalid_token",
        "provider_unauthorized",
        "oauth_refresh_token_missing",
    }
)

_T = TypeVar("_T")


def yandex_provider() -> MediaAwareYandexDirectProvider:
    enabled = str(os.getenv("CLIENTPLATFORM_AD_CONNECTIONS_ENABLED") or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        raise RuntimeError("advertising account connections are disabled")
    client_id = str(os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID") or "").strip()
    redirect_uri = str(os.getenv("CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI") or "").strip()
    if not client_id or not redirect_uri:
        raise RuntimeError("Yandex Direct provider is not configured")
    return MediaAwareYandexDirectProvider(
        oauth=YandexOAuthConfig(
            client_id=client_id,
            client_secret=str(
                os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET") or ""
            ).strip(),
            redirect_uri=redirect_uri,
        )
    )


def ad_vault() -> AdCredentialVault:
    return AgeAdCredentialVault()


def load_bundle(
    *,
    job: AdPublicationJob,
    vault: AdCredentialVault,
) -> tuple[AdConnection, YandexTokenBundle]:
    with get_db_ro() as conn:
        connection, token_json = AdWorkerStore(conn, vault=vault).load_active(
            business_id=job.business_id,
            connection_id=job.connection_id,
        )
    return connection, YandexTokenBundle.from_json(token_json)


def refresh_bundle(
    *,
    connection: AdConnection,
    bundle: YandexTokenBundle,
    provider: MediaAwareYandexDirectProvider,
    vault: AdCredentialVault,
) -> YandexTokenBundle:
    refreshed = provider.refresh(bundle=bundle)
    with get_db() as conn:
        AdWorkerStore(conn, vault=vault).replace_token_bundle(
            connection=connection,
            token_bundle_json=refreshed.to_json(),
        )
    return refreshed


def with_access_token(
    *,
    job: AdPublicationJob,
    operation: Callable[[str], _T],
    provider: MediaAwareYandexDirectProvider | None = None,
    vault: AdCredentialVault | None = None,
) -> _T:
    selected_provider = provider or yandex_provider()
    selected_vault = vault or ad_vault()
    connection, bundle = load_bundle(job=job, vault=selected_vault)
    try:
        return operation(bundle.access_token)
    except YandexDirectError as exc:
        if exc.code not in _AUTH_ERRORS or not bundle.refresh_token:
            raise
        refreshed = refresh_bundle(
            connection=connection,
            bundle=bundle,
            provider=selected_provider,
            vault=selected_vault,
        )
        return operation(refreshed.access_token)


__all__ = [
    "_AUTH_ERRORS",
    "ad_vault",
    "load_bundle",
    "refresh_bundle",
    "with_access_token",
    "yandex_provider",
]

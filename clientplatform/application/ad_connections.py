from __future__ import annotations

import json
import os
from dataclasses import dataclass

from clientplatform.domain.ad_connections import (
    AdConnection,
    AdConnectionInvariantViolation,
    AdProvider,
    AdPublicationJob,
    new_oauth_state,
    new_pkce_verifier,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.ad_connection_repository import AdConnectionRepository
from clientplatform.infrastructure.ad_credential_vault import (
    AdCredentialVault,
    AgeAdCredentialVault,
)
from clientplatform.infrastructure.promotion_repository import PromotionRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.integrations.yandex_direct import (
    YandexCampaign,
    YandexDirectError,
    YandexDirectProvider,
    YandexOAuthConfig,
    YandexTokenBundle,
)
from services.db import get_db, get_db_ro


@dataclass(frozen=True, slots=True)
class AdOAuthStart:
    provider: AdProvider
    authorization_url: str
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class AdOAuthCompletion:
    connection: AdConnection
    user_id: int


@dataclass(frozen=True, slots=True)
class AdPublicationDraft:
    job: AdPublicationJob
    campaign_name: str


def ad_connections_enabled() -> bool:
    return (os.getenv("CLIENTPLATFORM_AD_CONNECTIONS_ENABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def yandex_direct_provider_configured() -> bool:
    return bool((os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID") or "").strip())


def _redirect_uri() -> str:
    explicit = (os.getenv("CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI") or "").strip()
    if explicit:
        return explicit
    domain = (os.getenv("CLIENTPLATFORM_DOMAIN") or "").strip()
    if not domain:
        raise RuntimeError("CLIENTPLATFORM_DOMAIN is required for advertising OAuth")
    return f"https://{domain}/oauth/yandex-direct/callback"


def _provider() -> YandexDirectProvider:
    if not ad_connections_enabled():
        raise RuntimeError("advertising account connections are disabled")
    client_id = (os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID") or "").strip()
    if not client_id:
        raise RuntimeError("Yandex Direct OAuth application is not configured")
    return YandexDirectProvider(
        oauth=YandexOAuthConfig(
            client_id=client_id,
            client_secret=(
                os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET") or ""
            ).strip(),
            redirect_uri=_redirect_uri(),
        )
    )


def _vault() -> AdCredentialVault:
    return AgeAdCredentialVault()


def start_yandex_direct_oauth(
    *,
    actor: TenantContext,
    vault: AdCredentialVault | None = None,
    provider: YandexDirectProvider | None = None,
) -> AdOAuthStart:
    state = new_oauth_state()
    verifier = new_pkce_verifier()
    selected_vault = vault or _vault()
    selected_provider = provider or _provider()
    with get_db() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        AdConnectionRepository(conn, vault=selected_vault).create_oauth_session(
            actor=current,
            provider=AdProvider.YANDEX_DIRECT,
            state=state,
            verifier=verifier,
            ttl_seconds=600,
        )
    return AdOAuthStart(
        provider=AdProvider.YANDEX_DIRECT,
        authorization_url=selected_provider.authorization_url(
            state=state,
            verifier=verifier,
        ),
        expires_in_seconds=600,
    )


def complete_yandex_direct_oauth(
    *,
    state: str,
    code: str,
    vault: AdCredentialVault | None = None,
    provider: YandexDirectProvider | None = None,
) -> AdOAuthCompletion:
    selected_vault = vault or _vault()
    selected_provider = provider or _provider()
    with get_db() as conn:
        repository = AdConnectionRepository(conn, vault=selected_vault)
        session, verifier = repository.consume_oauth_session(state=state)
        if session.provider != AdProvider.YANDEX_DIRECT:
            raise AdConnectionInvariantViolation("OAuth provider does not match the callback")
        token = selected_provider.exchange_code(code=code, verifier=verifier)
        identity = selected_provider.account_identity(access_token=token.access_token)
        connection = repository.activate_oauth_connection(
            session=session,
            external_account_id=identity.account_id,
            external_login=identity.login,
            token_bundle_json=token.to_json(),
            permissions=("campaigns.read", "adgroups.write", "ads.write"),
        )
        return AdOAuthCompletion(connection=connection, user_id=session.user_id)


def list_ad_connections(
    *,
    actor: TenantContext,
    vault: AdCredentialVault | None = None,
) -> list[AdConnection]:
    with get_db_ro() as conn:
        return AdConnectionRepository(conn, vault=vault or _vault()).list_connections(
            actor=actor
        )


def list_yandex_direct_campaigns(
    *,
    actor: TenantContext,
    connection_id: str,
    vault: AdCredentialVault | None = None,
    provider: YandexDirectProvider | None = None,
) -> list[YandexCampaign]:
    selected_vault = vault or _vault()
    selected_provider = provider or _provider()
    with get_db_ro() as conn:
        repository = AdConnectionRepository(conn, vault=selected_vault)
        connection = repository.get_connection(actor=actor, connection_id=connection_id)
        if connection.provider != AdProvider.YANDEX_DIRECT:
            raise AdConnectionInvariantViolation("connection is not a Yandex Direct account")
        bundle = YandexTokenBundle.from_json(repository.token_bundle(connection=connection))
    return selected_provider.list_text_campaigns(access_token=bundle.access_token)


def create_ad_publication_draft(
    *,
    actor: TenantContext,
    promotion_campaign_id: str,
    connection_id: str,
    external_campaign_id: str,
    external_campaign_name: str,
    region_ids: tuple[int, ...],
    source_url: str,
    vault: AdCredentialVault | None = None,
) -> AdPublicationDraft:
    selected_vault = vault or _vault()
    with get_db() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        promotion = PromotionRepository(conn).get_campaign(
            actor=current,
            campaign_id=promotion_campaign_id,
        )
        creative = promotion.creative
        job = AdConnectionRepository(conn, vault=selected_vault).create_or_get_job(
            actor=current,
            promotion_campaign_id=promotion.id,
            connection_id=connection_id,
            external_campaign_id=external_campaign_id,
            external_campaign_name=external_campaign_name,
            region_ids=region_ids,
            source_url=source_url,
            title=creative.headline,
            text=creative.primary_text,
            creative_id=creative.creative_id,
        )
        return AdPublicationDraft(job=job, campaign_name=external_campaign_name)


def confirm_ad_publication(
    *,
    actor: TenantContext,
    job_id: str,
    vault: AdCredentialVault | None = None,
) -> AdPublicationJob:
    with get_db() as conn:
        return AdConnectionRepository(conn, vault=vault or _vault()).queue_job(
            actor=actor,
            job_id=job_id,
        )


def list_ad_publications(
    *,
    actor: TenantContext,
    vault: AdCredentialVault | None = None,
) -> list[AdPublicationJob]:
    with get_db_ro() as conn:
        return AdConnectionRepository(conn, vault=vault or _vault()).list_jobs(actor=actor)


def process_one_ad_publication(
    *,
    vault: AdCredentialVault | None = None,
    provider: YandexDirectProvider | None = None,
    max_attempts: int = 5,
) -> AdPublicationJob | None:
    selected_vault = vault or _vault()
    selected_provider = provider or _provider()
    with get_db() as conn:
        repository = AdConnectionRepository(conn, vault=selected_vault)
        claimed = repository.claim_due_job()
        if claimed is None:
            return None
        job, lock_token = claimed
        connection = repository._get_connection(  # worker-owned, tenant id comes from claimed row
            business_id=job.business_id,
            connection_id=job.connection_id,
        )
        try:
            bundle = YandexTokenBundle.from_json(repository.token_bundle(connection=connection))
            result = selected_provider.publish_text_ad(
                access_token=bundle.access_token,
                external_campaign_id=job.external_campaign_id,
                region_ids=job.region_ids,
                title=job.title,
                text=job.text,
                href=job.source_url,
                idempotency_key=job.idempotency_key,
            )
        except YandexDirectError as exc:
            return repository.fail_job(
                job=job,
                lock_token=lock_token,
                error_code=exc.code,
                retryable=exc.retryable,
                max_attempts=max_attempts,
            )
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
            return repository.fail_job(
                job=job,
                lock_token=lock_token,
                error_code="provider_runtime_failure",
                retryable=False,
                max_attempts=max_attempts,
            )
        return repository.complete_job(
            job=job,
            lock_token=lock_token,
            external_ad_group_id=result.ad_group_id,
            external_ad_id=result.ad_id,
        )


__all__ = [
    "AdOAuthCompletion",
    "AdOAuthStart",
    "AdPublicationDraft",
    "ad_connections_enabled",
    "complete_yandex_direct_oauth",
    "confirm_ad_publication",
    "create_ad_publication_draft",
    "list_ad_connections",
    "list_ad_publications",
    "list_yandex_direct_campaigns",
    "process_one_ad_publication",
    "start_yandex_direct_oauth",
    "yandex_direct_provider_configured",
]

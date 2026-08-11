from __future__ import annotations

import os
from dataclasses import dataclass

from clientplatform.application.ad_publication_assets import (
    get_asset_for_worker,
    read_asset_bytes,
    remember_provider_ids,
)
from clientplatform.domain.ad_connections import (
    AdConnectionError,
    AdPublicationJob,
    AdPublicationStatus,
)
from clientplatform.domain.ad_publication_assets import AdPublicationAssetKind
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.ad_connection_repository import AdConnectionRepository
from clientplatform.infrastructure.ad_credential_vault import (
    AdCredentialVault,
    AgeAdCredentialVault,
)
from clientplatform.infrastructure.ad_goal_publication_repository import (
    AdGoalPublicationRepository,
)
from clientplatform.infrastructure.ad_publication_asset_repository import (
    AdPublicationAssetRepository,
)
from clientplatform.infrastructure.ad_worker_store import AdWorkerStore
from clientplatform.integrations.yandex_direct import (
    YandexDirectError,
    YandexOAuthConfig,
    YandexTokenBundle,
)
from clientplatform.integrations.yandex_direct_media import (
    MediaAwareYandexDirectProvider,
)
from services.db import get_db, get_db_ro


_AUTH_ERRORS = {
    "provider_http_401",
    "provider_53",
    "provider_54",
    "provider_55",
    "provider_56",
    "provider_invalid_token",
    "provider_unauthorized",
    "oauth_refresh_token_missing",
}


class GoalPublicationBusy(AdConnectionError):
    """The exact idempotent draft is already being processed."""


@dataclass(frozen=True, slots=True)
class GoalPublicationResult:
    job: AdPublicationJob
    media_attached: bool
    media_pending: bool
    media_failed: bool = False


def _vault() -> AdCredentialVault:
    return AgeAdCredentialVault()


def _provider() -> MediaAwareYandexDirectProvider:
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


def _load_bundle(
    *,
    job: AdPublicationJob,
    vault: AdCredentialVault,
) -> tuple[object, YandexTokenBundle]:
    with get_db_ro() as conn:
        connection, token_json = AdWorkerStore(conn, vault=vault).load_active(
            business_id=job.business_id,
            connection_id=job.connection_id,
        )
    return connection, YandexTokenBundle.from_json(token_json)


def _refresh_bundle(
    *,
    connection: object,
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


def _publish_text(
    *,
    provider: MediaAwareYandexDirectProvider,
    bundle: YandexTokenBundle,
    job: AdPublicationJob,
):
    return provider.publish_text_ad(
        access_token=bundle.access_token,
        external_campaign_id=job.external_campaign_id,
        region_ids=job.region_ids,
        title=job.title,
        text=job.text,
        href=job.source_url,
        idempotency_key=job.idempotency_key,
    )


def _remember_media_error(*, job: AdPublicationJob, error_code: str) -> None:
    with get_db() as conn:
        AdPublicationAssetRepository(conn).remember_provider_error(
            business_id=job.business_id,
            publication_job_id=job.id,
            error_code=error_code,
        )


def _attach_media(
    *,
    provider: MediaAwareYandexDirectProvider,
    bundle: YandexTokenBundle,
    job: AdPublicationJob,
    ad_id: str,
) -> tuple[bool, bool, bool]:
    asset = get_asset_for_worker(
        business_id=job.business_id,
        publication_job_id=job.id,
    )
    if asset is None:
        return False, False, False
    if asset.provider_error_code:
        return False, False, True
    payload = read_asset_bytes(asset)
    if asset.kind == AdPublicationAssetKind.IMAGE:
        image_hash = asset.provider_image_hash
        if not image_hash:
            image_hash = provider.upload_image(
                access_token=bundle.access_token,
                payload=payload,
                name=asset.original_name,
            )
            remember_provider_ids(
                business_id=job.business_id,
                publication_job_id=job.id,
                provider_image_hash=image_hash,
            )
        provider.attach_image(
            access_token=bundle.access_token,
            ad_id=ad_id,
            image_hash=image_hash,
        )
        return True, False, False

    video_id = asset.provider_video_id
    if not video_id:
        video_id = provider.upload_video(
            access_token=bundle.access_token,
            payload=payload,
            name=asset.original_name,
        )
        asset = remember_provider_ids(
            business_id=job.business_id,
            publication_job_id=job.id,
            provider_video_id=video_id,
        ) or asset
    status = provider.video_status(
        access_token=bundle.access_token,
        video_id=video_id,
    )
    if status == "ERROR":
        _remember_media_error(job=job, error_code="ad_video_processing_failed")
        return False, False, True
    if status in {"NEW", "CONVERTING"}:
        return False, True, False
    creative_id = asset.provider_creative_id
    if not creative_id:
        creative_id = provider.create_video_extension(
            access_token=bundle.access_token,
            video_id=video_id,
        )
        remember_provider_ids(
            business_id=job.business_id,
            publication_job_id=job.id,
            provider_creative_id=creative_id,
        )
    provider.attach_video(
        access_token=bundle.access_token,
        ad_id=ad_id,
        creative_id=creative_id,
    )
    return True, False, False


def _sync_submitted_media(
    *,
    job: AdPublicationJob,
    vault: AdCredentialVault,
    provider: MediaAwareYandexDirectProvider,
) -> GoalPublicationResult:
    asset = get_asset_for_worker(
        business_id=job.business_id,
        publication_job_id=job.id,
    )
    if asset is None:
        return GoalPublicationResult(job=job, media_attached=False, media_pending=False)
    if asset.provider_error_code:
        return GoalPublicationResult(
            job=job,
            media_attached=False,
            media_pending=False,
            media_failed=True,
        )
    if not job.external_ad_id:
        raise AdConnectionError("submitted advertising draft is missing provider ad id")
    connection, bundle = _load_bundle(job=job, vault=vault)
    try:
        try:
            attached, pending, failed = _attach_media(
                provider=provider,
                bundle=bundle,
                job=job,
                ad_id=job.external_ad_id,
            )
        except YandexDirectError as exc:
            if exc.code not in _AUTH_ERRORS or not bundle.refresh_token:
                raise
            bundle = _refresh_bundle(
                connection=connection,
                bundle=bundle,
                provider=provider,
                vault=vault,
            )
            attached, pending, failed = _attach_media(
                provider=provider,
                bundle=bundle,
                job=job,
                ad_id=job.external_ad_id,
            )
    except (OSError, RuntimeError, TypeError, ValueError, YandexDirectError) as exc:
        raise AdConnectionError("provider_media_sync_failure") from exc
    return GoalPublicationResult(
        job=job,
        media_attached=attached,
        media_pending=pending,
        media_failed=failed,
    )


def submit_goal_publication(
    *,
    actor: TenantContext,
    job_id: str,
    vault: AdCredentialVault | None = None,
    provider: MediaAwareYandexDirectProvider | None = None,
) -> GoalPublicationResult:
    """Create exactly this owner's Yandex DRAFT and attach selected media.

    This operation never sends the DRAFT to moderation and never authorizes
    spend. Those remain in the separate consent-bound launch vertical.
    """

    selected_vault = vault or _vault()
    selected_provider = provider or _provider()
    with get_db() as conn:
        repository = AdGoalPublicationRepository(conn)
        claim = repository.claim(actor=actor, job_id=job_id)
        if claim is None:
            current = repository.get(actor=actor, job_id=job_id)
            if current.status == AdPublicationStatus.SUBMITTED:
                return _sync_submitted_media(
                    job=current,
                    vault=selected_vault,
                    provider=selected_provider,
                )
            raise GoalPublicationBusy("advertising draft is already being processed")
    job, lock_token = claim.job, claim.lock_token
    connection, bundle = _load_bundle(job=job, vault=selected_vault)
    try:
        try:
            result = _publish_text(
                provider=selected_provider,
                bundle=bundle,
                job=job,
            )
            media_attached, media_pending, media_failed = _attach_media(
                provider=selected_provider,
                bundle=bundle,
                job=job,
                ad_id=result.ad_id,
            )
        except YandexDirectError as exc:
            if exc.code not in _AUTH_ERRORS or not bundle.refresh_token:
                raise
            bundle = _refresh_bundle(
                connection=connection,
                bundle=bundle,
                provider=selected_provider,
                vault=selected_vault,
            )
            result = _publish_text(
                provider=selected_provider,
                bundle=bundle,
                job=job,
            )
            media_attached, media_pending, media_failed = _attach_media(
                provider=selected_provider,
                bundle=bundle,
                job=job,
                ad_id=result.ad_id,
            )
    except YandexDirectError as exc:
        with get_db() as conn:
            failed = AdConnectionRepository(conn, vault=selected_vault).fail_job(
                job=job,
                lock_token=lock_token,
                error_code=exc.code,
                retryable=exc.retryable,
                max_attempts=5,
            )
        raise AdConnectionError(failed.last_error_code or "provider_failure") from exc
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        with get_db() as conn:
            AdConnectionRepository(conn, vault=selected_vault).fail_job(
                job=job,
                lock_token=lock_token,
                error_code="provider_runtime_failure",
                retryable=False,
                max_attempts=5,
            )
        raise AdConnectionError("provider_runtime_failure") from exc

    with get_db() as conn:
        submitted = AdConnectionRepository(conn, vault=selected_vault).complete_job(
            job=job,
            lock_token=lock_token,
            external_ad_group_id=result.ad_group_id,
            external_ad_id=result.ad_id,
        )
    return GoalPublicationResult(
        job=submitted,
        media_attached=media_attached,
        media_pending=media_pending,
        media_failed=media_failed,
    )


def process_one_pending_video_asset(
    *,
    vault: AdCredentialVault | None = None,
    provider: MediaAwareYandexDirectProvider | None = None,
) -> bool:
    """Finish one persisted Yandex video extension after provider conversion.

    Permanent conversion errors are recorded and skipped on future ticks so one
    bad user video can never block media processing for other tenants.
    """

    selected_vault = vault or _vault()
    selected_provider = provider or _provider()
    with get_db_ro() as conn:
        row = conn.execute(
            """
            SELECT a.business_id, a.publication_job_id, a.provider_video_id,
                   a.provider_creative_id, j.connection_id, j.external_ad_id
            FROM ad_publication_assets a
            JOIN ad_publication_jobs j
              ON j.id=a.publication_job_id AND j.business_id=a.business_id
            WHERE a.kind='video' AND a.provider_video_id IS NOT NULL
              AND a.provider_error_code IS NULL
              AND j.status='submitted' AND j.external_ad_id IS NOT NULL
              AND a.provider_creative_id IS NULL
            ORDER BY a.updated_at, a.publication_job_id
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return False

        def value(key: str, position: int):
            return row[key] if hasattr(row, "keys") else row[position]

        business_id = str(value("business_id", 0))
        publication_job_id = str(value("publication_job_id", 1))
        video_id = str(value("provider_video_id", 2))
        connection_id = str(value("connection_id", 4))
        ad_id = str(value("external_ad_id", 5))
        connection, token_json = AdWorkerStore(conn, vault=selected_vault).load_active(
            business_id=business_id,
            connection_id=connection_id,
        )
    bundle = YandexTokenBundle.from_json(token_json)
    try:
        status = selected_provider.video_status(
            access_token=bundle.access_token,
            video_id=video_id,
        )
    except YandexDirectError as exc:
        if exc.code not in _AUTH_ERRORS or not bundle.refresh_token:
            return False
        bundle = _refresh_bundle(
            connection=connection,
            bundle=bundle,
            provider=selected_provider,
            vault=selected_vault,
        )
        status = selected_provider.video_status(
            access_token=bundle.access_token,
            video_id=video_id,
        )
    if status == "ERROR":
        with get_db() as conn:
            AdPublicationAssetRepository(conn).remember_provider_error(
                business_id=business_id,
                publication_job_id=publication_job_id,
                error_code="ad_video_processing_failed",
            )
        return True
    if status != "READY":
        return False
    creative_id = selected_provider.create_video_extension(
        access_token=bundle.access_token,
        video_id=video_id,
    )
    selected_provider.attach_video(
        access_token=bundle.access_token,
        ad_id=ad_id,
        creative_id=creative_id,
    )
    remember_provider_ids(
        business_id=business_id,
        publication_job_id=publication_job_id,
        provider_creative_id=creative_id,
    )
    return True


__all__ = [
    "GoalPublicationBusy",
    "GoalPublicationResult",
    "process_one_pending_video_asset",
    "submit_goal_publication",
]

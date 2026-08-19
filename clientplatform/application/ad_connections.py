from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from clientplatform.domain.ad_connections import (
    AdConnection,
    AdConnectionInvariantViolation,
    AdProvider,
    AdPublicationJob,
    new_oauth_state,
    new_pkce_verifier,
    normalize_external_campaign_id,
)
from clientplatform.domain.managed_ad_campaigns import (
    ManagedAdCampaign,
    ManagedAdCampaignStatus,
    managed_campaign_name,
    managed_campaign_provisioning_key,
    normalize_managed_campaign_error,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.ad_connection_repository import AdConnectionRepository
from clientplatform.infrastructure.ad_credential_vault import (
    AdCredentialVault,
    AgeAdCredentialVault,
)
from clientplatform.infrastructure.ad_oauth_completion_store import (
    AdOAuthCompletionReservation,
    AdOAuthCompletionStore,
)
from clientplatform.infrastructure.ad_worker_store import (
    AdConnectionLifecycleStore,
    AdWorkerStore,
)
from clientplatform.infrastructure.promotion_repository import PromotionRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.integrations.yandex_direct import (
    YandexCampaign,
    YandexDirectError,
    YandexDirectProvider,
    YandexOAuthConfig,
    YandexPublicationResult,
    YandexTokenBundle,
)
from clientplatform.integrations.yandex_direct_moderation import (
    ModeratingYandexDirectProvider,
)
from clientplatform.integrations.yandex_oauth_lifecycle import YandexOAuthLifecycle
from services.db import get_db, get_db_ro


_ACCOUNT_ATTENTION_ERRORS = {
    "provider_http_401",
    "provider_53",
    "provider_54",
    "provider_55",
    "provider_56",
    "provider_invalid_token",
    "provider_unauthorized",
    "oauth_refresh_token_missing",
}
_REUSABLE_OAUTH_CODE_ERRORS = frozenset(
    {"provider_invalid_grant", "provider_bad_verification_code"}
)
_MANAGED_SELECT = """
    SELECT id, business_id, promotion_campaign_id, connection_id, provider,
           provisioning_key, external_campaign_id, external_campaign_name,
           status, last_error_code, created_by_member_id, created_at, updated_at
    FROM ad_managed_campaigns
"""


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


def _client_id() -> str:
    return (os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID") or "").strip()


def _client_secret() -> str:
    return (os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET") or "").strip()


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
    client_id = _client_id()
    if not client_id:
        raise RuntimeError("Yandex Direct OAuth application is not configured")
    return ModeratingYandexDirectProvider(
        oauth=YandexOAuthConfig(
            client_id=client_id,
            client_secret=_client_secret(),
            redirect_uri=_redirect_uri(),
        )
    )


def _vault() -> AdCredentialVault:
    return AgeAdCredentialVault()


def _auth_error(exc: YandexDirectError) -> bool:
    return exc.code in _ACCOUNT_ATTENTION_ERRORS


def _refresh_token_bundle(
    *,
    provider: YandexDirectProvider,
    vault: AdCredentialVault,
    connection: AdConnection,
    bundle: YandexTokenBundle,
) -> YandexTokenBundle:
    refreshed = provider.refresh(bundle=bundle)
    with get_db() as conn:
        AdWorkerStore(conn, vault=vault).replace_token_bundle(
            connection=connection,
            token_bundle_json=refreshed.to_json(),
        )
    return refreshed


def _managed_from_row(row) -> ManagedAdCampaign:
    return ManagedAdCampaign(
        id=str(row[0]),
        business_id=str(row[1]),
        promotion_campaign_id=str(row[2]),
        connection_id=str(row[3]),
        provider=AdProvider(str(row[4])),
        provisioning_key=str(row[5]),
        external_campaign_id=None if row[6] is None else str(row[6]),
        external_campaign_name=str(row[7]),
        status=ManagedAdCampaignStatus(str(row[8])),
        last_error_code=None if row[9] is None else str(row[9]),
        created_by_member_id=str(row[10]),
        created_at=str(row[11]),
        updated_at=str(row[12]),
    )


def _managed_get(
    conn,
    *,
    business_id: str,
    promotion_campaign_id: str,
    connection_id: str,
) -> ManagedAdCampaign | None:
    row = conn.execute(
        _MANAGED_SELECT
        + " WHERE business_id=? AND promotion_campaign_id=? AND connection_id=? LIMIT 1",
        (business_id, promotion_campaign_id, connection_id),
    ).fetchone()
    return None if row is None else _managed_from_row(row)


def _reserve_managed_campaign(
    conn,
    *,
    actor: TenantContext,
    promotion_campaign_id: str,
    connection_id: str,
) -> tuple[ManagedAdCampaign, bool]:
    promotion_id = normalize_uuid(
        promotion_campaign_id,
        field_name="promotion_campaign_id",
    )
    connection_id = normalize_uuid(connection_id, field_name="ad_connection_id")
    key = managed_campaign_provisioning_key(
        business_id=actor.business_id,
        promotion_campaign_id=promotion_id,
        connection_id=connection_id,
    )
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cursor = conn.execute(
        """
        INSERT INTO ad_managed_campaigns(
            id, business_id, promotion_campaign_id, connection_id, provider,
            provisioning_key, external_campaign_id, external_campaign_name,
            status, last_error_code, created_by_member_id, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, NULL, ?, 'provisioning', NULL, ?, ?, ?)
        ON CONFLICT(business_id, promotion_campaign_id, connection_id) DO NOTHING
        """,
        (
            str(uuid4()),
            actor.business_id,
            promotion_id,
            connection_id,
            AdProvider.YANDEX_DIRECT.value,
            key,
            managed_campaign_name(key),
            actor.membership_id,
            stamp,
            stamp,
        ),
    )
    managed = _managed_get(
        conn,
        business_id=actor.business_id,
        promotion_campaign_id=promotion_id,
        connection_id=connection_id,
    )
    if managed is None:
        raise AdConnectionInvariantViolation("managed campaign reservation failed")
    if managed.provisioning_key != key:
        raise AdConnectionInvariantViolation("managed campaign ownership marker mismatch")
    return managed, int(cursor.rowcount or 0) == 1


def _claim_failed_managed_creation(
    conn,
    *,
    managed: ManagedAdCampaign,
) -> bool:
    """Atomically reacquire only a provider-confirmed failed creation attempt."""

    if managed.status != ManagedAdCampaignStatus.FAILED:
        return False
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cursor = conn.execute(
        """
        UPDATE ad_managed_campaigns
        SET status='provisioning', last_error_code=NULL, updated_at=?
        WHERE id=? AND business_id=? AND status='failed' AND updated_at=?
        """,
        (
            stamp,
            managed.id,
            managed.business_id,
            managed.updated_at,
        ),
    )
    return int(cursor.rowcount or 0) == 1


def _bind_managed_campaign(
    conn,
    *,
    managed: ManagedAdCampaign,
    external_campaign_id: str,
) -> ManagedAdCampaign:
    external_id = normalize_external_campaign_id(external_campaign_id)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        cursor = conn.execute(
            """
            UPDATE ad_managed_campaigns
            SET external_campaign_id=?, status='ready', last_error_code=NULL,
                updated_at=?
            WHERE id=? AND business_id=? AND status IN ('provisioning', 'failed')
            """,
            (external_id, stamp, managed.id, managed.business_id),
        )
    except sqlite3.IntegrityError as exc:
        raise AdConnectionInvariantViolation(
            "external campaign is already bound to another promotion"
        ) from exc
    current = _managed_get(
        conn,
        business_id=managed.business_id,
        promotion_campaign_id=managed.promotion_campaign_id,
        connection_id=managed.connection_id,
    )
    if current is None:
        raise AdConnectionInvariantViolation("managed campaign binding disappeared")
    if int(cursor.rowcount or 0) != 1 and not (
        current.status == ManagedAdCampaignStatus.READY
        and current.external_campaign_id == external_id
    ):
        raise AdConnectionInvariantViolation("managed campaign provisioning lease was lost")
    return current


def _mark_managed_failure(
    *,
    managed: ManagedAdCampaign,
    error_code: str,
    uncertain: bool,
) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_db() as conn:
        conn.execute(
            """
            UPDATE ad_managed_campaigns
            SET status=?, last_error_code=?, updated_at=?
            WHERE id=? AND business_id=? AND status!='ready'
            """,
            (
                "provisioning" if uncertain else "failed",
                normalize_managed_campaign_error(error_code),
                stamp,
                managed.id,
                managed.business_id,
            ),
        )


def _mark_managed_refresh_failure(
    *,
    managed: ManagedAdCampaign,
    exc: BaseException,
) -> None:
    error_code = exc.code if isinstance(exc, YandexDirectError) else "provider_refresh_failure"
    _mark_managed_failure(
        managed=managed,
        error_code=error_code,
        uncertain=False,
    )


def _settle_failed_oauth_completion(
    *,
    reservation: AdOAuthCompletionReservation,
    vault: AdCredentialVault,
    reusable: bool,
) -> None:
    with get_db() as conn:
        store = AdOAuthCompletionStore(conn, vault=vault)
        if reusable:
            store.release(reservation=reservation)
        else:
            store.consume(reservation=reservation)


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

    # Reserve the one-time OAuth state in a short committed transaction. The
    # provider token and identity requests below must never hold a DB connection
    # or transaction; a bounded durable lease prevents concurrent completion and
    # is reclaimable after a crashed worker.
    with get_db() as conn:
        reservation = AdOAuthCompletionStore(
            conn,
            vault=selected_vault,
        ).reserve(state=state)
    session = reservation.session
    if session.provider != AdProvider.YANDEX_DIRECT:
        _settle_failed_oauth_completion(
            reservation=reservation,
            vault=selected_vault,
            reusable=False,
        )
        raise AdConnectionInvariantViolation("OAuth provider does not match the callback")

    try:
        token = selected_provider.exchange_code(
            code=code,
            verifier=reservation.verifier,
        )
        identity = selected_provider.account_identity(access_token=token.access_token)
    except YandexDirectError as exc:
        _settle_failed_oauth_completion(
            reservation=reservation,
            vault=selected_vault,
            reusable=exc.code in _REUSABLE_OAUTH_CODE_ERRORS,
        )
        raise
    except Exception:
        _settle_failed_oauth_completion(
            reservation=reservation,
            vault=selected_vault,
            reusable=False,
        )
        raise

    try:
        with get_db() as conn:
            repository = AdConnectionRepository(conn, vault=selected_vault)
            current = TenancyRepository(conn).resolve_context(
                user_id=session.user_id,
                business_id=session.business_id,
            )
            current.assert_can_manage_ad_connections()
            if current.membership_id != session.membership_id:
                raise AdConnectionInvariantViolation(
                    "OAuth membership changed before the callback completed"
                )
            consumed_session = AdOAuthCompletionStore(
                conn,
                vault=selected_vault,
            ).consume(reservation=reservation)
            connection = repository.activate_oauth_connection(
                session=consumed_session,
                external_account_id=identity.account_id,
                external_login=identity.login,
                token_bundle_json=token.to_json(),
                permissions=("campaigns.read", "adgroups.write", "ads.write"),
            )
    except Exception:
        # The final local transaction rolled back, so burn the reservation in a
        # separate short transaction when it is still ours. If another worker
        # reclaimed an expired lease, the lease-lost invariant is safer than
        # overwriting that worker's ownership.
        try:
            _settle_failed_oauth_completion(
                reservation=reservation,
                vault=selected_vault,
                reusable=False,
            )
        except AdConnectionInvariantViolation:
            pass
        raise
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
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        connection = AdConnectionRepository(
            conn,
            vault=selected_vault,
        ).get_connection(actor=current, connection_id=connection_id)
        if connection.provider != AdProvider.YANDEX_DIRECT:
            raise AdConnectionInvariantViolation("connection is not a Yandex Direct account")
        connection, token_json = AdWorkerStore(
            conn,
            vault=selected_vault,
        ).load_active(
            business_id=current.business_id,
            connection_id=connection.id,
        )
    bundle = YandexTokenBundle.from_json(token_json)
    try:
        return selected_provider.list_text_campaigns(access_token=bundle.access_token)
    except YandexDirectError as exc:
        if not _auth_error(exc) or not bundle.refresh_token:
            raise
        refreshed = _refresh_token_bundle(
            provider=selected_provider,
            vault=selected_vault,
            connection=connection,
            bundle=bundle,
        )
        return selected_provider.list_text_campaigns(
            access_token=refreshed.access_token
        )


def ensure_yandex_managed_campaign(
    *,
    actor: TenantContext,
    promotion_campaign_id: str,
    connection_id: str,
    vault: AdCredentialVault | None = None,
    provider: YandexDirectProvider | None = None,
) -> ManagedAdCampaign:
    selected_vault = vault or _vault()
    selected_provider = provider or _provider()
    with get_db() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        promotion = PromotionRepository(conn).get_campaign(
            actor=current,
            campaign_id=promotion_campaign_id,
        )
        connection = AdConnectionRepository(
            conn,
            vault=selected_vault,
        ).get_connection(actor=current, connection_id=connection_id)
        if connection.provider != AdProvider.YANDEX_DIRECT:
            raise AdConnectionInvariantViolation("connection is not a Yandex Direct account")
        managed, created = _reserve_managed_campaign(
            conn,
            actor=current,
            promotion_campaign_id=promotion.id,
            connection_id=connection.id,
        )
        _connection, token_json = AdWorkerStore(
            conn,
            vault=selected_vault,
        ).load_active(
            business_id=current.business_id,
            connection_id=connection.id,
        )
    if managed.status == ManagedAdCampaignStatus.READY:
        return managed
    bundle = YandexTokenBundle.from_json(token_json)

    try:
        found = selected_provider.find_managed_campaign(
            access_token=bundle.access_token,
            campaign_name=managed.external_campaign_name,
        )
    except YandexDirectError as exc:
        if _auth_error(exc) and bundle.refresh_token:
            try:
                bundle = _refresh_token_bundle(
                    provider=selected_provider,
                    vault=selected_vault,
                    connection=connection,
                    bundle=bundle,
                )
                found = selected_provider.find_managed_campaign(
                    access_token=bundle.access_token,
                    campaign_name=managed.external_campaign_name,
                )
            except YandexDirectError as retry_exc:
                _mark_managed_refresh_failure(managed=managed, exc=retry_exc)
                raise
            except OSError as retry_exc:
                _mark_managed_refresh_failure(managed=managed, exc=retry_exc)
                raise
            except RuntimeError as retry_exc:
                _mark_managed_refresh_failure(managed=managed, exc=retry_exc)
                raise
            except ValueError as retry_exc:
                _mark_managed_refresh_failure(managed=managed, exc=retry_exc)
                raise
        else:
            _mark_managed_failure(
                managed=managed,
                error_code=exc.code,
                uncertain=False,
            )
            raise
    if found is not None:
        with get_db() as conn:
            return _bind_managed_campaign(
                conn,
                managed=managed,
                external_campaign_id=found.campaign_id,
            )

    owns_creation = created
    if not owns_creation:
        with get_db() as conn:
            current_managed = _managed_get(
                conn,
                business_id=managed.business_id,
                promotion_campaign_id=managed.promotion_campaign_id,
                connection_id=managed.connection_id,
            )
            if current_managed is None:
                raise AdConnectionInvariantViolation(
                    "managed campaign reservation disappeared"
                )
            if current_managed.status == ManagedAdCampaignStatus.READY:
                return current_managed
            owns_creation = _claim_failed_managed_creation(
                conn,
                managed=current_managed,
            )
            managed = current_managed
    if not owns_creation:
        raise AdConnectionInvariantViolation(
            "managed campaign provisioning is already in progress"
        )

    try:
        external_id = selected_provider.create_disabled_managed_campaign(
            access_token=bundle.access_token,
            campaign_name=managed.external_campaign_name,
        )
    except YandexDirectError as exc:
        if _auth_error(exc) and bundle.refresh_token:
            try:
                bundle = _refresh_token_bundle(
                    provider=selected_provider,
                    vault=selected_vault,
                    connection=connection,
                    bundle=bundle,
                )
            except YandexDirectError as refresh_exc:
                _mark_managed_refresh_failure(managed=managed, exc=refresh_exc)
                raise
            except OSError as refresh_exc:
                _mark_managed_refresh_failure(managed=managed, exc=refresh_exc)
                raise
            except RuntimeError as refresh_exc:
                _mark_managed_refresh_failure(managed=managed, exc=refresh_exc)
                raise
            except ValueError as refresh_exc:
                _mark_managed_refresh_failure(managed=managed, exc=refresh_exc)
                raise
            try:
                external_id = selected_provider.create_disabled_managed_campaign(
                    access_token=bundle.access_token,
                    campaign_name=managed.external_campaign_name,
                )
            except YandexDirectError as retry_exc:
                _mark_managed_failure(
                    managed=managed,
                    error_code=retry_exc.code,
                    uncertain=retry_exc.retryable,
                )
                raise
        else:
            _mark_managed_failure(
                managed=managed,
                error_code=exc.code,
                uncertain=exc.retryable,
            )
            raise

    with get_db() as conn:
        return _bind_managed_campaign(
            conn,
            managed=managed,
            external_campaign_id=external_id,
        )


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


def create_managed_ad_publication_draft(
    *,
    actor: TenantContext,
    promotion_campaign_id: str,
    connection_id: str,
    region_ids: tuple[int, ...],
    source_url: str,
    vault: AdCredentialVault | None = None,
    provider: YandexDirectProvider | None = None,
) -> AdPublicationDraft:
    managed = ensure_yandex_managed_campaign(
        actor=actor,
        promotion_campaign_id=promotion_campaign_id,
        connection_id=connection_id,
        vault=vault,
        provider=provider,
    )
    if managed.external_campaign_id is None:
        raise AdConnectionInvariantViolation("managed campaign is not ready")
    return create_ad_publication_draft(
        actor=actor,
        promotion_campaign_id=promotion_campaign_id,
        connection_id=connection_id,
        external_campaign_id=managed.external_campaign_id,
        external_campaign_name=managed.external_campaign_name,
        region_ids=region_ids,
        source_url=source_url,
        vault=vault,
    )


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


def disconnect_ad_connection(
    *,
    actor: TenantContext,
    connection_id: str,
    vault: AdCredentialVault | None = None,
    oauth_lifecycle: YandexOAuthLifecycle | None = None,
) -> AdConnection:
    selected_vault = vault or _vault()
    lifecycle = oauth_lifecycle or YandexOAuthLifecycle(
        client_id=_client_id(),
        client_secret=_client_secret(),
    )
    with get_db() as conn:
        connection, token_json = AdConnectionLifecycleStore(
            conn,
            vault=selected_vault,
        ).begin_disconnect(
            actor=actor,
            connection_id=connection_id,
        )
    if connection.provider != AdProvider.YANDEX_DIRECT:
        raise AdConnectionInvariantViolation("unsupported advertising provider")

    with get_db_ro() as conn:
        if AdWorkerStore(conn, vault=selected_vault).has_publishing_job(
            business_id=connection.business_id,
            connection_id=connection.id,
        ):
            raise AdConnectionInvariantViolation(
                "advertising publication is still in progress; retry disconnect"
            )

    if token_json:
        bundle = YandexTokenBundle.from_json(token_json)
        result = lifecycle.revoke(access_token=bundle.access_token)
        if not result.local_erasure_allowed:
            raise AdConnectionInvariantViolation(
                "provider did not allow local credential erasure"
            )
    with get_db() as conn:
        return AdConnectionLifecycleStore(
            conn,
            vault=selected_vault,
        ).erase_after_provider_revocation(
            actor=actor,
            connection_id=connection.id,
        )


def list_ad_publications(
    *,
    actor: TenantContext,
    vault: AdCredentialVault | None = None,
) -> list[AdPublicationJob]:
    with get_db_ro() as conn:
        return AdConnectionRepository(conn, vault=vault or _vault()).list_jobs(actor=actor)


def _fail_claimed_job(
    *,
    vault: AdCredentialVault,
    job: AdPublicationJob,
    lock_token: str,
    error_code: str,
    retryable: bool,
    max_attempts: int,
) -> AdPublicationJob:
    with get_db() as conn:
        failed = AdConnectionRepository(conn, vault=vault).fail_job(
            job=job,
            lock_token=lock_token,
            error_code=error_code,
            retryable=retryable,
            max_attempts=max_attempts,
        )
        if error_code not in _ACCOUNT_ATTENTION_ERRORS:
            AdWorkerStore(conn, vault=vault).keep_available_after_job_failure(
                business_id=job.business_id,
                connection_id=job.connection_id,
            )
        return failed


def _managed_name_for_job(job: AdPublicationJob) -> str | None:
    with get_db_ro() as conn:
        row = conn.execute(
            """
            SELECT external_campaign_name
            FROM ad_managed_campaigns
            WHERE business_id=? AND promotion_campaign_id=? AND connection_id=?
              AND external_campaign_id=? AND status='ready'
            LIMIT 1
            """,
            (
                job.business_id,
                job.promotion_campaign_id,
                job.connection_id,
                job.external_campaign_id,
            ),
        ).fetchone()
    return None if row is None else str(row[0])


def _publish_job(
    *,
    provider: YandexDirectProvider,
    token: str,
    job: AdPublicationJob,
    managed_name: str | None,
) -> YandexPublicationResult:
    if managed_name is not None:
        return provider.publish_managed_text_ad(
            access_token=token,
            external_campaign_id=job.external_campaign_id,
            expected_campaign_name=managed_name,
            region_ids=job.region_ids,
            title=job.title,
            text=job.text,
            href=job.source_url,
            idempotency_key=job.idempotency_key,
        )
    return provider.publish_text_ad(
        access_token=token,
        external_campaign_id=job.external_campaign_id,
        region_ids=job.region_ids,
        title=job.title,
        text=job.text,
        href=job.source_url,
        idempotency_key=job.idempotency_key,
    )


def process_one_ad_publication(
    *,
    vault: AdCredentialVault | None = None,
    provider: YandexDirectProvider | None = None,
    max_attempts: int = 5,
) -> AdPublicationJob | None:
    selected_vault = vault or _vault()
    selected_provider = provider or _provider()
    with get_db() as conn:
        AdWorkerStore(conn, vault=selected_vault).recover_stale_publication_leases()
        claimed = AdConnectionRepository(
            conn,
            vault=selected_vault,
        ).claim_due_job()
    if claimed is None:
        return None
    job, lock_token = claimed
    managed_name = _managed_name_for_job(job)

    try:
        with get_db_ro() as conn:
            connection, token_json = AdWorkerStore(
                conn,
                vault=selected_vault,
            ).load_active(
                business_id=job.business_id,
                connection_id=job.connection_id,
            )
        bundle = YandexTokenBundle.from_json(token_json)
        try:
            result = _publish_job(
                provider=selected_provider,
                token=bundle.access_token,
                job=job,
                managed_name=managed_name,
            )
        except YandexDirectError as exc:
            if not _auth_error(exc) or not bundle.refresh_token:
                raise
            refreshed = _refresh_token_bundle(
                provider=selected_provider,
                vault=selected_vault,
                connection=connection,
                bundle=bundle,
            )
            result = _publish_job(
                provider=selected_provider,
                token=refreshed.access_token,
                job=job,
                managed_name=managed_name,
            )
    except YandexDirectError as exc:
        return _fail_claimed_job(
            vault=selected_vault,
            job=job,
            lock_token=lock_token,
            error_code=exc.code,
            retryable=exc.retryable,
            max_attempts=max_attempts,
        )
    except (OSError, RuntimeError, ValueError, TypeError):
        return _fail_claimed_job(
            vault=selected_vault,
            job=job,
            lock_token=lock_token,
            error_code="provider_runtime_failure",
            retryable=False,
            max_attempts=max_attempts,
        )

    with get_db() as conn:
        return AdConnectionRepository(conn, vault=selected_vault).complete_job(
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
    "create_managed_ad_publication_draft",
    "disconnect_ad_connection",
    "ensure_yandex_managed_campaign",
    "list_ad_connections",
    "list_ad_publications",
    "list_yandex_direct_campaigns",
    "process_one_ad_publication",
    "start_yandex_direct_oauth",
    "yandex_direct_provider_configured",
]
from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timezone

from clientplatform.domain.ad_spend import (
    AdSpendAuthorization,
    AdSpendAuthorizationStatus,
    AdSpendInvariantViolation,
)
from clientplatform.domain.ad_spend_operations import (
    AdSpendOperation,
    AdSpendOperationType,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.ad_credential_vault import (
    AdCredentialVault,
    AgeAdCredentialVault,
)
from clientplatform.infrastructure.ad_spend_operation_repository import (
    AdSpendOperationContext,
    AdSpendOperationRepository,
)
from clientplatform.infrastructure.ad_spend_operation_supersession import (
    complete_superseded_launch,
    launch_is_superseded_by_stop,
)
from clientplatform.infrastructure.ad_spend_repository import AdSpendRepository
from clientplatform.integrations.yandex_direct import (
    YandexDirectError,
    YandexOAuthConfig,
    YandexTokenBundle,
)
from clientplatform.integrations.yandex_direct_actions import YandexDirectAdActions
from services.db import get_db, get_db_ro


PreMutationGuard = Callable[[AdSpendOperationContext, datetime], bool]


def ad_spend_mutations_enabled() -> bool:
    return (
        os.getenv("CLIENTPLATFORM_AD_SPEND_MUTATIONS_ENABLED") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def _provider() -> YandexDirectAdActions:
    client_id = (os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID") or "").strip()
    redirect_uri = (os.getenv("CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI") or "").strip()
    if not client_id or not redirect_uri:
        raise AdSpendInvariantViolation("Yandex Direct provider is not configured")
    return YandexDirectAdActions(
        oauth=YandexOAuthConfig(
            client_id=client_id,
            client_secret=(
                os.getenv("CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET") or ""
            ).strip(),
            redirect_uri=redirect_uri,
        )
    )


def _current_launch_authorization(
    context: AdSpendOperationContext,
) -> AdSpendAuthorization:
    with get_db_ro() as conn:
        authorization, _version = AdSpendRepository(conn)._get_with_version(  # noqa: SLF001
            business_id=context.operation.business_id,
            authorization_id=context.operation.authorization_id,
        )
    if authorization.status != AdSpendAuthorizationStatus.LAUNCHING:
        raise AdSpendInvariantViolation("launch authorization is no longer current")
    receipt = authorization.consent_receipt
    if receipt is None or receipt.receipt_hash != context.receipt_hash:
        raise AdSpendInvariantViolation("launch consent receipt changed")
    if authorization.connection_id != context.connection_id:
        raise AdSpendInvariantViolation("launch connection changed")
    if authorization.external_campaign_id != context.external_campaign_id:
        raise AdSpendInvariantViolation("launch campaign changed")
    if authorization.currency != context.currency:
        raise AdSpendInvariantViolation("launch currency changed")
    if authorization.hard_cap_minor != context.hard_cap_minor:
        raise AdSpendInvariantViolation("launch hard cap changed")
    if authorization.daily_cap_minor != context.daily_cap_minor:
        raise AdSpendInvariantViolation("launch daily cap changed")
    if authorization.authorization_expires_at != context.authorization_expires_at:
        raise AdSpendInvariantViolation("launch expiry changed")
    return authorization


def queue_ad_spend_launch(
    *,
    actor: TenantContext,
    authorization_id: str,
) -> AdSpendOperation:
    if not ad_spend_mutations_enabled():
        raise AdSpendInvariantViolation("advertising spend mutations are disabled")
    with get_db() as conn:
        return AdSpendOperationRepository(conn).enqueue_launch(
            actor=actor,
            authorization_id=authorization_id,
        )


def queue_ad_spend_stop(
    *,
    actor: TenantContext,
    authorization_id: str,
) -> AdSpendOperation:
    with get_db() as conn:
        return AdSpendOperationRepository(conn).enqueue_stop(
            actor=actor,
            authorization_id=authorization_id,
        )


def _superseded_evidence(
    *,
    evidence: dict[str, object] | None,
    error: AdSpendInvariantViolation | YandexDirectError | ValueError,
) -> dict[str, object]:
    if evidence is not None:
        return evidence
    error_code = (
        error.code if isinstance(error, YandexDirectError) else type(error).__name__.lower()
    )
    return {
        "operation": "launch",
        "provider_error_code": error_code,
        "provider_outcome_unknown": isinstance(error, YandexDirectError),
    }


def process_one_ad_spend_operation(
    *,
    pre_mutation_guard: PreMutationGuard | None,
    vault: AdCredentialVault | None = None,
    provider: YandexDirectAdActions | None = None,
    max_attempts: int = 8,
) -> AdSpendOperation | None:
    selected_vault = vault or AgeAdCredentialVault()
    selected_provider = provider or _provider()
    with get_db() as conn:
        repository = AdSpendOperationRepository(conn, vault=selected_vault)
        repository.recover_stale_leases()
        operation = repository.claim_due()
    if operation is None:
        return None

    provider_evidence: dict[str, object] | None = None
    try:
        with get_db_ro() as conn:
            context, token_json = AdSpendOperationRepository(
                conn,
                vault=selected_vault,
            ).load_claimed_context(operation=operation)
        now = datetime.now(timezone.utc)
        authorization = None
        if operation.operation_type == AdSpendOperationType.LAUNCH:
            if not ad_spend_mutations_enabled():
                raise AdSpendInvariantViolation(
                    "advertising spend mutations are disabled"
                )
            if pre_mutation_guard is None or not pre_mutation_guard(context, now):
                raise AdSpendInvariantViolation(
                    "fresh server-side spend guard rejected launch"
                )
            authorization = _current_launch_authorization(context)
        bundle = YandexTokenBundle.from_json(token_json)
        activation = None
        if operation.operation_type == AdSpendOperationType.LAUNCH:
            if authorization is None:
                raise AdSpendInvariantViolation("launch authorization is missing")
            activation = selected_provider.configure_managed_launch_budget(
                access_token=bundle.access_token,
                external_campaign_id=context.external_campaign_id,
                hard_cap_minor=context.hard_cap_minor,
                daily_cap_minor=context.daily_cap_minor,
                currency=context.currency,
                expected_snapshot_strategy=authorization.snapshot.strategy,
                client_login=context.external_login,
            )
            # A revocation/stop may race the provider budget write. Re-load the
            # immutable consent state before the spend-capable moderation call.
            _current_launch_authorization(context)
            now = datetime.now(timezone.utc)
            result = selected_provider.moderate_ad(
                access_token=bundle.access_token,
                external_ad_id=context.external_ad_id,
                expected_campaign_id=context.external_campaign_id,
                captured_at=now,
                client_login=context.external_login,
            )
        else:
            result = selected_provider.suspend_ad(
                access_token=bundle.access_token,
                external_ad_id=context.external_ad_id,
                expected_campaign_id=context.external_campaign_id,
                captured_at=now,
                client_login=context.external_login,
            )
        provider_evidence = {
            "operation": result.operation,
            "ad_id": result.after.ad_id,
            "campaign_id": result.after.campaign_id,
            "state": result.after.state,
            "status": result.after.status,
            "provider_version": result.after.provider_version,
            "reconciled_without_mutation": result.reconciled_without_mutation,
        }
        if activation is not None:
            provider_evidence.update(
                {
                    "campaign_type": activation.campaign_type,
                    "weekly_spend_limit_micros": activation.weekly_spend_limit_micros,
                    "budget_reconciled_without_mutation": (
                        activation.reconciled_without_mutation
                    ),
                }
            )
        with get_db() as conn:
            return AdSpendOperationRepository(
                conn,
                vault=selected_vault,
            ).complete(
                operation=operation,
                provider_evidence=provider_evidence,
                now=now,
            )
    except (AdSpendInvariantViolation, YandexDirectError, ValueError) as exc:
        now = datetime.now(timezone.utc)
        if operation.operation_type == AdSpendOperationType.LAUNCH:
            with get_db() as conn:
                if launch_is_superseded_by_stop(conn, operation=operation):
                    return complete_superseded_launch(
                        conn,
                        operation=operation,
                        provider_evidence=_superseded_evidence(
                            evidence=provider_evidence,
                            error=exc,
                        ),
                        now=now,
                    )
        retryable = isinstance(exc, YandexDirectError) and bool(exc.retryable)
        error_code = (
            exc.code if isinstance(exc, YandexDirectError) else type(exc).__name__.lower()
        )
        with get_db() as conn:
            return AdSpendOperationRepository(
                conn,
                vault=selected_vault,
            ).fail(
                operation=operation,
                error_code=error_code,
                retryable=retryable,
                max_attempts=max_attempts,
                now=now,
            )


__all__ = [
    "PreMutationGuard",
    "ad_spend_mutations_enabled",
    "process_one_ad_spend_operation",
    "queue_ad_spend_launch",
    "queue_ad_spend_stop",
]

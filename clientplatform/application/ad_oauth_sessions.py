from __future__ import annotations

from clientplatform.domain.ad_connections import AdProvider
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.ad_oauth_session_store import AdOAuthSessionStore
from services.db import get_db


def cancel_yandex_direct_oauth(
    *,
    actor: TenantContext,
    state: str,
) -> bool:
    """Consume an outstanding Yandex OAuth state without contacting the provider."""

    with get_db() as conn:
        return AdOAuthSessionStore(conn).cancel(
            actor=actor,
            provider=AdProvider.YANDEX_DIRECT,
            state=state,
        )


__all__ = ["cancel_yandex_direct_oauth"]

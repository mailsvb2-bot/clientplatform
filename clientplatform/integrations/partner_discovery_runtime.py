from __future__ import annotations

from collections.abc import Callable
from typing import Any

from clientplatform.domain.partners import PartnerChannel
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.integrations.partner_discovery import (
    CompositePartnerDiscovery,
    DiscoveredPartner,
    PartnerDiscoveryProviderError,
    PartnerDiscoveryQuery,
)
from clientplatform.runtime.secrets import (
    ClientPlatformCredentialProvider,
    CredentialProvider,
    SecretReferenceError,
)
from services.db import get_db_ro
from services.messenger.provider_transport import (
    ProviderPermanentHTTPError,
    form_request,
)


VkRequest = Callable[[str, dict[str, Any]], dict[str, Any]]


class VkConnectionPartnerDiscoveryProvider:
    """Read-only VK community discovery backed by a tenant Connection.

    The provider keeps only a protected credential reference. The raw secret is
    resolved immediately before the provider request and is never returned,
    persisted or included in an error string.
    """

    def __init__(
        self,
        *,
        connection_id: str,
        credential_reference: str,
        credential_provider: CredentialProvider,
        request: VkRequest = form_request,
        api_version: str = "5.199",
    ) -> None:
        self._connection_id = str(connection_id)
        self._credential_reference = str(credential_reference)
        self._credentials = credential_provider
        self._request = request
        self._api_version = str(api_version or "5.199")

    @property
    def provider_name(self) -> str:
        return "vk_connection_communities"

    def discover(self, query: PartnerDiscoveryQuery) -> list[DiscoveredPartner]:
        try:
            credential = self._credentials.resolve(self._credential_reference)
        except SecretReferenceError as exc:
            raise PartnerDiscoveryProviderError(
                "vk partner discovery credential unavailable"
            ) from exc
        if not str(credential or "").strip():
            raise PartnerDiscoveryProviderError(
                "vk partner discovery credential unavailable"
            )

        result: list[DiscoveredPartner] = []
        per_term = max(1, min(100, query.limit // max(1, len(query.terms)) + 1))
        for term in query.terms:
            try:
                payload = self._request(
                    "https://api.vk.com/method/groups.search",
                    {
                        "q": str(term),
                        "count": per_term,
                        "access_token": credential,
                        "v": self._api_version,
                    },
                )
            except ProviderPermanentHTTPError as exc:
                raise PartnerDiscoveryProviderError(
                    "vk partner discovery transport unavailable"
                ) from exc
            except ConnectionError as exc:
                raise PartnerDiscoveryProviderError(
                    "vk partner discovery transport unavailable"
                ) from exc
            except TimeoutError as exc:
                raise PartnerDiscoveryProviderError(
                    "vk partner discovery transport unavailable"
                ) from exc
            except OSError as exc:
                raise PartnerDiscoveryProviderError(
                    "vk partner discovery transport unavailable"
                ) from exc
            if not isinstance(payload, dict):
                raise PartnerDiscoveryProviderError(
                    "vk partner discovery returned an invalid payload"
                )
            if payload.get("error"):
                error = payload.get("error")
                code = "provider_error"
                if isinstance(error, dict):
                    code = str(error.get("error_code") or "provider_error")[:32]
                raise PartnerDiscoveryProviderError(
                    f"vk partner discovery rejected request: {code}"
                )
            response = payload.get("response")
            if not isinstance(response, dict):
                raise PartnerDiscoveryProviderError(
                    "vk partner discovery response is missing"
                )
            items = response.get("items")
            if not isinstance(items, list):
                raise PartnerDiscoveryProviderError(
                    "vk partner discovery items are missing"
                )
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = " ".join(str(item.get("name") or "").split()).strip()
                screen_name = str(item.get("screen_name") or "").strip()
                group_id = str(item.get("id") or "").strip()
                if not name or not (screen_name or group_id):
                    continue
                slug = screen_name or f"club{group_id}"
                description = " ".join(
                    str(item.get("description") or "").split()
                ).strip()
                result.append(
                    DiscoveredPartner(
                        name=name,
                        source_url=f"https://vk.com/{slug}",
                        audience_summary=(
                            description[:1000]
                            if description
                            else f"Публичное VK-сообщество найдено по запросу «{term}»"
                        ),
                        channel=PartnerChannel.VK,
                        tags=(str(term), "vk"),
                    )
                )
                if len(result) >= query.limit:
                    return result
        return result


def build_connected_partner_discovery(
    *,
    actor: TenantContext,
    credential_provider: CredentialProvider | None = None,
    request: VkRequest = form_request,
) -> CompositePartnerDiscovery:
    """Compose only provider connections owned by the active tenant."""

    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_promotions()
        rows = conn.execute(
            """
            SELECT id, credential_reference
            FROM connections
            WHERE business_id=? AND platform='vk'
              AND connection_type='vk_community' AND status='active'
            ORDER BY created_at,id
            """,
            (current.business_id,),
        ).fetchall()

    credentials = credential_provider or ClientPlatformCredentialProvider()
    providers = [
        VkConnectionPartnerDiscoveryProvider(
            connection_id=str(row["id"] if hasattr(row, "keys") else row[0]),
            credential_reference=str(
                row["credential_reference"] if hasattr(row, "keys") else row[1]
            ),
            credential_provider=credentials,
            request=request,
        )
        for row in rows
    ]
    return CompositePartnerDiscovery(providers)


__all__ = [
    "VkConnectionPartnerDiscoveryProvider",
    "build_connected_partner_discovery",
]

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from clientplatform.domain.partners import ContactBasis, PartnerChannel


class PartnerDiscoveryProviderError(RuntimeError):
    """A configured provider failed in a recoverable, provider-local way."""


class PartnerDiscoveryUnavailable(RuntimeError):
    """Every configured discovery provider failed for the current request."""


@dataclass(frozen=True, slots=True)
class DiscoveredPartner:
    name: str
    source_url: str
    audience_summary: str = ""
    recent_topic: str = ""
    follower_count: int | None = None
    tags: tuple[str, ...] = ()
    channel: PartnerChannel = PartnerChannel.MANUAL
    contact_value: str = ""
    contact_basis: ContactBasis = ContactBasis.UNKNOWN
    competitor: bool = False


@dataclass(frozen=True, slots=True)
class PartnerDiscoveryQuery:
    terms: tuple[str, ...]
    limit: int = 50
    country: str = ""
    language: str = "ru"

    def __post_init__(self) -> None:
        terms = tuple(
            dict.fromkeys(
                " ".join(str(item or "").split()).strip() for item in self.terms
            )
        )
        terms = tuple(item for item in terms if item)
        if not terms:
            raise ValueError("partner discovery requires at least one search term")
        limit = int(self.limit)
        if limit < 1 or limit > 500:
            raise ValueError("partner discovery limit must be between 1 and 500")
        object.__setattr__(self, "terms", terms[:32])
        object.__setattr__(self, "limit", limit)


class PartnerDiscoveryProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    def discover(self, query: PartnerDiscoveryQuery) -> Sequence[DiscoveredPartner]: ...


class CompositePartnerDiscovery:
    """Read-only fan-out; only declared provider failures are isolated."""

    def __init__(self, providers: Sequence[PartnerDiscoveryProvider]) -> None:
        self._providers = tuple(providers)

    @property
    def configured(self) -> bool:
        return bool(self._providers)

    def discover(self, query: PartnerDiscoveryQuery) -> list[DiscoveredPartner]:
        unique: dict[str, DiscoveredPartner] = {}
        failed_providers: list[str] = []
        succeeded = 0
        for provider in self._providers:
            try:
                rows = provider.discover(query)
            except PartnerDiscoveryProviderError:
                failed_providers.append(
                    str(getattr(provider, "provider_name", "unknown"))
                )
                continue
            succeeded += 1
            for row in rows:
                key = _fingerprint(row)
                existing = unique.get(key)
                if existing is None or _completeness(row) > _completeness(existing):
                    unique[key] = row
                if len(unique) >= query.limit:
                    return list(unique.values())
        if self._providers and succeeded == 0:
            providers = ",".join(failed_providers) or "unknown"
            raise PartnerDiscoveryUnavailable(
                f"partner discovery unavailable: {providers}"
            )
        return list(unique.values())


class VkPublicCommunitySearchProvider:
    """Normalize a connector-owned read-only VK community search callable.

    This adapter owns no token, secret or HTTP stack. Public discovery evidence
    is never treated as permission to message the community automatically.
    """

    _RECOVERABLE_ERRORS = (ConnectionError, TimeoutError, OSError)

    def __init__(
        self,
        *,
        search: Callable[[str, int], Sequence[Mapping[str, Any]]],
    ) -> None:
        if not callable(search):
            raise TypeError("VK partner discovery requires a read-only search callable")
        self._search = search

    @property
    def provider_name(self) -> str:
        return "vk_public_communities"

    def discover(self, query: PartnerDiscoveryQuery) -> Sequence[DiscoveredPartner]:
        result: list[DiscoveredPartner] = []
        per_term = max(1, min(100, query.limit // max(1, len(query.terms)) + 1))
        for term in query.terms:
            try:
                items = self._search(str(term), per_term)
            except self._RECOVERABLE_ERRORS as exc:
                raise PartnerDiscoveryProviderError(
                    "vk community search unavailable"
                ) from exc
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                screen_name = str(item.get("screen_name") or "").strip()
                group_id = str(item.get("id") or "").strip()
                name = " ".join(str(item.get("name") or "").split()).strip()
                if not name or not (screen_name or group_id):
                    continue
                slug = screen_name or f"club{group_id}"
                result.append(
                    DiscoveredPartner(
                        name=name,
                        source_url=f"https://vk.com/{slug}",
                        audience_summary=(
                            f"Публичное VK-сообщество найдено по запросу «{term}»"
                        ),
                        tags=(term, "vk"),
                        channel=PartnerChannel.VK,
                        contact_basis=ContactBasis.UNKNOWN,
                    )
                )
                if len(result) >= query.limit:
                    return tuple(result)
        return tuple(result)


class StaticPartnerDiscoveryProvider:
    """Deterministic provider for tests, imports and curated seed lists."""

    def __init__(
        self,
        rows: Sequence[DiscoveredPartner],
        *,
        name: str = "static",
    ) -> None:
        self._rows = tuple(rows)
        self._name = str(name)

    @property
    def provider_name(self) -> str:
        return self._name

    def discover(self, query: PartnerDiscoveryQuery) -> Sequence[DiscoveredPartner]:
        terms = tuple(item.casefold() for item in query.terms)
        out: list[DiscoveredPartner] = []
        for row in self._rows:
            haystack = " ".join(
                (row.name, row.audience_summary, row.recent_topic, *row.tags)
            ).casefold()
            if any(term in haystack for term in terms):
                out.append(row)
            if len(out) >= query.limit:
                break
        return out


def _fingerprint(row: DiscoveredPartner) -> str:
    url = str(row.source_url or "").strip().casefold()
    return url or (
        str(row.channel)
        + ":"
        + str(row.contact_value or row.name).strip().casefold()
    )


def _completeness(row: DiscoveredPartner) -> int:
    return sum(
        bool(value)
        for value in (
            row.name,
            row.source_url,
            row.audience_summary,
            row.recent_topic,
            row.contact_value,
            row.tags,
            row.follower_count is not None,
        )
    )


__all__ = [
    "CompositePartnerDiscovery",
    "DiscoveredPartner",
    "PartnerDiscoveryProvider",
    "PartnerDiscoveryProviderError",
    "PartnerDiscoveryUnavailable",
    "PartnerDiscoveryQuery",
    "StaticPartnerDiscoveryProvider",
    "VkPublicCommunitySearchProvider",
]

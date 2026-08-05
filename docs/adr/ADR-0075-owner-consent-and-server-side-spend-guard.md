# ADR-0075: Owner consent and server-side spend guard

## Status

Accepted.

## Context

A provider-created advertising DRAFT and a fresh budget snapshot are not consent to spend money. The owner must see and confirm the exact immutable terms, and every launch/continuation decision must be re-evaluated server-side.

## Decision

ClientPlatform exposes application services to request consent, confirm consent with exact `terms_hash` and `snapshot_hash`, inspect the consent view, and revoke authorization.

A pure server-side guard blocks launch or continued activity when any of these conditions is true:

- authorization is revoked or expired;
- provider evidence is stale;
- account/campaign/currency no longer match;
- provider no longer reports launch eligibility;
- hard cap or daily cap is reached;
- authorization is not in an explicitly spend-capable state.

Frontend or Telegram payloads may echo hashes, but cannot define or alter limits. The authoritative terms are always loaded from tenant-scoped persistence.

## Safety boundary

This ADR does not enable provider mutation. `Ads.moderate`, `Ads.resume`, `Campaigns.resume`, and any operation capable of impressions or spend remain absent from runtime wiring. A later outbox worker must call this guard immediately before every provider mutation and before any retry after an unknown result.

# ADR-0072: Consent-bound advertising spend

## Status

Accepted as the domain foundation for Issue #74. Runtime provider mutations remain disabled until persistence, reconciliation, stop enforcement and exact-head CI are implemented.

## Context

PR #73 deliberately limited Yandex Direct integration to creating remote `DRAFT` objects. Confirmation of draft creation is not consent to moderation, impressions or spending.

A future launch path must fail closed when the provider state is stale, the account is ambiguous, the tenant does not match, the actor is not the business owner, or the requested amount exceeds a provider-derived limit.

## Decision

ClientPlatform introduces a separate consent-bound spend aggregate with three immutable components:

1. `ProviderBudgetSnapshot` — provider-derived account, campaign, currency, budget, current spend, strategy, eligibility, version and validity window;
2. `AdSpendAuthorization` — tenant-scoped hard cap, daily cap, regions, expiry and explicit state machine;
3. `AdSpendConsentReceipt` — owner identity, exact canonical terms, snapshot hash, timestamp and versioned receipt hash.

The allowed state flow is:

```text
draft -> awaiting_consent -> authorized -> launching -> active
                                      \-> revoked/expired
launching|active -> stopping -> stopped
launching|stopping -> failed
```

A spend-capable state cannot exist without a receipt matching the same business, authorization, provider snapshot and canonical terms. Only a `PlatformRole.OWNER` tenant context can request or grant consent. Administrator access is intentionally insufficient.

Money is represented only as integer minor units. The authorization expiry cannot exceed the provider snapshot validity. The hard cap cannot exceed the provider-reported available budget. A stale, future-dated or non-launch-eligible snapshot is rejected.

## Consequences

This ADR does not enable real advertising spend. It creates the invariant boundary that persistence and workers must preserve.

The next slices must add:

- tenant-scoped persistence and immutable receipt storage;
- atomic compare-and-set transitions and launch/stop leases;
- provider budget reads and reconciliation before every mutation;
- hard-cap, daily-cap, expiry and revocation stop enforcement;
- owner-facing Telegram confirmation with exact amounts and consequences;
- concurrency, restart, cross-tenant and unknown-provider-result tests.

No existing draft-confirmation action may be reused as spend consent.

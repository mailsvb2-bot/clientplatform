# ADR-0071: Personal advertising account hub

**Status:** Accepted for implementation, provider rollout disabled by default  
**Date:** 2026-08-05

## Context

ClientPlatform already turns an open booking slot into a safe promotion creative,
a durable source link and an attributable `opened → booked` result. The missing
step is provider delivery: each business must be able to connect its own
advertising account and explicitly submit the creative to that account.

Millions of businesses do not require millions of ClientPlatform applications.
ClientPlatform registers one OAuth application per provider; every business
creates an isolated authorization grant for its own external account.

BusinesAIOS contains useful patterns for provider catalogs, encrypted credentials,
health gates, idempotent execution and audit evidence. It is not imported as a
runtime, package, database or network dependency. DecisionCore, MarketGraph,
autonomous budget allocation and marketing autopilot remain outside
ClientPlatform.

## Decision

Introduce a provider-neutral Ad Connection Hub above the existing Promotion
Engine and below external advertising APIs.

The first provider is Yandex Direct:

1. An owner or administrator starts OAuth authorization.
2. ClientPlatform stores only a SHA-256 hash of `state` and an age-encrypted PKCE
   verifier for ten minutes.
3. The callback consumes the state exactly once, exchanges the code and stores an
   age-encrypted token bundle outside browser-visible state.
4. The owner chooses one existing text campaign from the connected account.
5. The owner supplies explicit region IDs and reviews the generated ad.
6. A separate confirmation creates an idempotent durable publication job.
7. A background worker reconciles a deterministic ad group and destination URL
   before creating remote objects.
8. Provider IDs and status are stored alongside the existing Promotion Engine
   source link, preserving booking attribution.

ClientPlatform does not create or change campaign budgets, bidding strategies or
payment settings in this vertical. Existing campaign settings remain the user's
responsibility.

## Security and tenancy

- Every connection, OAuth session, publication job and audit event is scoped by
  `business_id`.
- Only owners and administrators may connect or disconnect external accounts.
- Marketers may create promotion content but cannot attach new financial access.
- Access and refresh tokens are never stored in plaintext in PostgreSQL.
- Production requires a separately provisioned age identity mounted read-only
  into the application container.
- Production never generates the identity automatically.
- OAuth state is random, stored only as a hash and consumed once.
- PKCE uses S256.
- Provider errors are reduced to bounded codes; tokens and provider response
  bodies are excluded from logs and audit records.
- Publication execution is leased and idempotent. Duplicate Telegram callbacks
  cannot create duplicate spending objects.
- Disconnect removes local encrypted credentials and cancels unsubmitted jobs.
  It does not claim to stop advertisements already running at the provider.

## Availability

The provider feature is disabled unless all of these are valid:

- `CLIENTPLATFORM_AD_CONNECTIONS_ENABLED=1`;
- Yandex OAuth client ID and secret;
- exact HTTPS callback URL for the configured ClientPlatform domain;
- the expected read-only age identity path;
- the expected host secret directory.

The existing Promotion Engine and booking flow remain available when the provider
is disabled or unavailable.

## Consequences

ClientPlatform remains a scheduling, booking and client-acquisition product, not
a general autonomous marketing operating system. Additional providers must
implement the same connection, credential, idempotency, audit and confirmation
contracts in separate reviewed changes.

Production rollout requires registering and moderating the ClientPlatform OAuth
application with Yandex, provisioning the age identity, enabling the feature and
performing a real account smoke test. Merging this ADR does not perform that
rollout.

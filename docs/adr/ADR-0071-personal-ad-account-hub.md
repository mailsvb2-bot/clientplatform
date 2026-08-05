# ADR-0071: Personal advertising account hub

**Status:** Accepted for implementation, provider rollout disabled by default  
**Date:** 2026-08-05  
**Safety revision:** 2026-08-05

## Context

ClientPlatform already turns an open booking slot into a safe promotion creative,
a durable source link and an attributable `opened → booked` result. The missing
step is provider delivery: each business must be able to connect its own
advertising account and transfer the creative into that account.

Millions of businesses do not require millions of ClientPlatform applications.
ClientPlatform registers one OAuth application per provider; every business
creates an isolated authorization grant for its own external account.

BusinesAIOS contains useful patterns for provider catalogs, encrypted credentials,
health gates, idempotent execution and audit evidence. It is not imported as a
runtime, package, database or network dependency. DecisionCore, MarketGraph,
autonomous budget allocation, bidding and marketing autopilot remain outside
ClientPlatform.

A post-implementation adversarial review found that “submit to an existing
campaign” was not a sufficient financial boundary. Creating a keyword and
sending an ad to moderation can make it eligible to consume the existing
campaign budget, even when ClientPlatform does not change the campaign's budget
or strategy. A Telegram confirmation therefore cannot be treated as permission
to start spending until ClientPlatform can show and enforce a provider-derived
budget snapshot, a time window and a hard spending cap.

## Decision

Introduce a provider-neutral Ad Connection Hub above the existing Promotion
Engine and below external advertising APIs.

The first provider is Yandex Direct:

1. An owner or administrator starts OAuth authorization.
2. ClientPlatform stores only a SHA-256 hash of `state` and an age-encrypted PKCE
   verifier for ten minutes.
3. The callback consumes the state exactly once and exchanges the code.
4. The connected advertising identity is resolved through Yandex Direct
   `Clients.get`, not through a generic Yandex profile endpoint.
5. Archived, ambiguous and read-only Direct identities are rejected before the
   token is activated in ClientPlatform.
6. The owner chooses an existing active, accepted `TEXT_CAMPAIGN`, supplies
   explicit regions and reviews the generated copy.
7. A separate confirmation creates an idempotent durable publication job.
8. The worker reconciles a deterministic ad group, exact narrow keyword and ad,
   then verifies that the remote ad remains in `DRAFT`.
9. ClientPlatform never calls `Ads.moderate` in this vertical. The user reviews
   and launches the draft in Yandex Direct manually.
10. Provider IDs and status are stored alongside the existing Promotion Engine
    source link, preserving booking attribution.

`UNIFIED_CAMPAIGN` is deliberately unsupported in this vertical because its
placement, targeting and budget semantics require a separate explicit contract.

ClientPlatform does not create or change campaign budgets, bidding strategies,
payment settings or moderation state in this vertical.

## Security, privacy and tenancy

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
  cannot create duplicate remote drafts.
- Disconnect immediately blocks new work and cancels unsubmitted jobs.
- Local encrypted credentials are erased even when provider revocation is
  temporarily unavailable. Provider revocation is best-effort and its safe error
  code may be audited, but it never blocks the user's local-erasure request.
- A disconnect racing with a worker may leave an orphan remote **draft**, but it
  cannot leave a newly moderated or spending ad. The lost lease prevents the
  worker from reporting that draft as successfully submitted.

## Explicit non-goals

This vertical does not:

- submit an ad to moderation;
- enable impressions;
- set or inherit a spend authorization;
- create a unified campaign;
- manage bids, strategies or payments;
- promise that a draft is live advertising.

A future launch vertical must first implement and test all of the following:

- provider-derived budget and strategy snapshot;
- explicit maximum amount and end time;
- immutable confirmation receipt containing those values;
- provider-side reconciliation before activation;
- automatic stop when the advertised slot is booked or expires;
- spend and status synchronization;
- emergency kill switch and audit trail.

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

Production rollout requires registering and approving the ClientPlatform OAuth
application with Yandex, provisioning the age identity, enabling the feature and
performing a sandbox and real-account **draft-only** smoke test. Merging this ADR
does not perform that rollout.

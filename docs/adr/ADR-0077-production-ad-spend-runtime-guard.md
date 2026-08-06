# ADR-0077: Production ad-spend runtime guard and automatic stop monitor

## Status

Accepted for implementation behind disabled production feature flags.

## Context

ADR-0072 through ADR-0076 established tenant-scoped advertising accounts, read-only provider evidence, immutable owner consent, server-side caps and an idempotent launch/stop outbox. The remaining safety gap was runtime composition: the provider mutation worker did not yet receive a fresh financial guard, active authorizations were not periodically rechecked, and stuck launch/stop operations were not exposed through readiness diagnostics.

## Decision

ClientPlatform will use one durable advertising runtime for draft publication and consent-bound spend operations.

Before every launch mutation it must:

1. reload the immutable authorization and consent receipt from PostgreSQL;
2. verify the exact connection, campaign, currency, caps, expiry and receipt hash held by the leased operation;
3. read the campaign budget and current-day spend from Yandex Direct again;
4. derive the provider report date from the explicitly configured IANA timezone `CLIENTPLATFORM_YANDEX_DIRECT_REPORT_TIMEZONE`;
5. reject account changes, provider-day rollover, counter regression, stale evidence, provider ineligibility, expired consent, daily-cap exhaustion and hard-cap exhaustion;
6. mutate only the exact provider advertisement already bound to the operation.

The worker also periodically scans `launching` and `active` authorizations. A failed guard queues the existing idempotent stop operation. Provider read failures fail closed and therefore also queue a stop rather than silently allowing continued spend.

Health and readiness expose:

- whether the advertising runtime is configured and running;
- its last tick and recent errors;
- numbers of processed publication and spend operations;
- guard scans, allowed authorizations, automatic stops and fail-closed outcomes;
- queued, retrying, processing, failed, stale and overdue spend operations.

## Safety boundary

`CLIENTPLATFORM_AD_CONNECTIONS_ENABLED` remains disabled by default. `CLIENTPLATFORM_AD_SPEND_MUTATIONS_ENABLED` is added to production environment preparation with a default of `0`. Enabling mutations without advertising connections is rejected. Enabling advertising connections requires an explicit valid provider-report timezone.

The runtime does not call campaign resume, change budgets, change bids, expand targeting, alter regions or create autonomous bidding behavior. Stop remains available even while the mutation kill switch is disabled.

## Consequences

A configured advertising runtime can no longer remain readiness-green when its worker is stopped, its report timezone is invalid, its operation outbox is unavailable, or recent/stale operations exceed operator thresholds.

Provider report failures can stop advertising earlier than necessary. This is intentional: uncertain financial evidence must fail closed.

Actual production activation still requires operator-provided Yandex OAuth credentials, the account timezone, credential encryption identity and a separate deliberate change of the feature flags. Merging this ADR does not activate advertising or spend money.

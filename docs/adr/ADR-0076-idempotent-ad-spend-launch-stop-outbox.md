# ADR-0076: Idempotent ad spend launch and stop outbox

## Status

Accepted.

## Decision

Launch and stop are represented as tenant-scoped durable operations with unique `(business, authorization, operation_type)` identity, compare-and-set authorization transitions, leases, stale-lease recovery, bounded retries and provider reconciliation.

Launch may call only `Ads.moderate` for the exact provider-created DRAFT after a fresh server-side guard. Stop may call only `Ads.suspend` for the exact ad. The adapter never resumes campaigns or ads, changes bids, strategies, budgets, keywords or regions.

The worker reads provider state before mutation and treats an already submitted or suspended ad as successful reconciliation. An unknown result is retried only through the same read-before-mutate path.

## Kill switch

`CLIENTPLATFORM_AD_SPEND_MUTATIONS_ENABLED` defaults to disabled. Launch enqueue and launch processing fail closed while disabled. Stop remains available so an operator or owner can remove serving capability even after the launch kill switch is turned off.

## Runtime boundary

This change provides the durable operation service but does not enable the mutation flag in production configuration. Production activation requires explicit owner configuration after staging verification and a concrete pre-mutation guard that refreshes provider budget evidence immediately before launch.

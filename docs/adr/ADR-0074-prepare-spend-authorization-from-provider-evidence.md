# ADR-0074: Prepare spend authorization from fresh provider evidence

## Status

Accepted as the application boundary for Issue #74. This slice prepares and persists authorization drafts only. It does not request consent, enqueue launch commands or mutate Yandex Direct.

## Context

The domain model, atomic persistence and read-only Yandex financial adapter now exist independently. They must be connected without creating a time-of-check/time-of-use gap that could bind an authorization to another tenant, another campaign, a revoked connection or an unsubmitted local publication job.

OAuth credentials must not remain inside an open database transaction while external provider requests are in flight. Conversely, provider evidence must be revalidated against current local state before persistence.

## Decision

ClientPlatform introduces `prepare_ad_spend_authorization` with three phases:

1. **Read-only local phase**
   - resolve current membership and require `PlatformRole.OWNER`;
   - load exactly one tenant-scoped `SUBMITTED` publication job;
   - require an active Yandex Direct connection;
   - capture account, login, campaign and region identities;
   - decrypt the active token bundle.
2. **External read-only phase**
   - read exact campaign funds, status, payment state and strategies;
   - read exact campaign/date daily net cost;
   - refresh OAuth once only for recognized authentication failures, then repeat both reads;
   - reconcile a short-lived `ProviderBudgetSnapshot` without provider mutation.
3. **Write phase**
   - call the atomic `AdSpendRepository.create_or_get_draft` boundary;
   - recheck the submitted job, active connection, tenant, campaign and snapshot binding inside the write transaction;
   - persist only a launch-eligible snapshot and bounded authorization.

The authorization validity is limited to 1–300 seconds and cannot exceed the snapshot validity. The report date is an internal server-side value and must remain within one day of current UTC to accommodate provider account-day boundaries without accepting arbitrary historical input.

A pending or failed report produces no local authorization. A changed account or connection blocks provider reads or persistence. Refreshing OAuth updates only the encrypted token bundle and restarts both evidence reads so campaign and cost evidence come from one credential generation.

## Consequences

This service is not wired to Telegram, HTTP or a worker. It cannot start impressions or spending. Future slices must add:

- provider-time-zone derivation instead of an explicitly supplied internal report date;
- owner-facing exact-terms confirmation and immutable receipt creation;
- reconciliation evidence persistence for each future mutation attempt;
- a separate idempotent launch/stop outbox with leases and unknown-result recovery;
- continuous hard-cap, daily-cap, expiry and revocation enforcement.

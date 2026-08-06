# ADR-0078: Automatic expiry enforcement for unused ad-spend authorizations

## Status

Accepted for implementation behind the disabled advertising runtime flags.

## Context

The launch guard already rejects an authorization after `authorization_expires_at`, and active advertising is stopped by the periodic provider guard. An unused authorization could nevertheless remain stored as `draft`, `awaiting_consent` or `authorized` after its deadline, making operator and owner views misleading even though a later launch would fail closed.

## Decision

The advertising runtime periodically selects due, non-active authorizations in the states:

- `draft`;
- `awaiting_consent`;
- `authorized`.

Each record moves to `expired` through a tenant-scoped compare-and-set update using its exact previous status and row version. The transition records `authorization_expired`, increments `row_version` and writes a bounded audit event with the previous state.

`launching` and `active` authorizations are not silently marked expired. They continue through the existing provider reconciliation and durable stop path so that database state cannot claim completion before the exact Yandex advertisement is known to be safe.

## Consequences

Expired unused consent disappears from actionable launch controls without requiring an owner callback. A concurrent owner launch, revoke or consent action wins or loses through the existing state and row-version checks rather than being overwritten.

The change does not enable advertising connections, provider mutations or production deployment. The production defaults remain fail-closed.

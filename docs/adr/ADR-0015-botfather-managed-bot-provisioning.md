# ADR-0015: BotFather managed bot provisioning boundary

## Status

Accepted — 2026-07-29.

Acceptance evidence was recorded on exact implementation head `d876c865270982a8f20613507c6e54ae45fbf463`:

- all twelve repository workflows completed successfully;
- the dedicated Bot Provisioning workflow passed its unit and orchestration wall;
- PostgreSQL 16 passed two-connection races for idempotent request creation, exclusive fresh-lease acquisition and exclusive stale-lease recovery;
- Production Isolation created the provisioning schema with the least-privilege application role, included the table in the custom-format dump and completed the disposable PostgreSQL 16 restore drill;
- Release Gate passed regression, critical type contracts, Bandit, dependency audit and release hygiene;
- CI passed regression, PostgreSQL, static/security and both coverage ratchets.

## Context

ADR-0014 established a tenant-safe Managed Bot Gateway, but it deliberately did not create or connect Telegram bot accounts. Telegram's ordinary Bot API cannot create a new bot account. A business owner can create one through BotFather, but copying its raw token through ClientPlatform forms, callbacks, logs or database rows would break the secret-reference boundary.

Provisioning also spans two failure domains: Telegram webhook configuration and the ClientPlatform database. Holding a database transaction open while calling Telegram is unsafe, while configuring Telegram first can leave an orphaned webhook if the database commit later fails. Double clicks and process crashes must not create duplicate routes or permanently strand a request.

## Decision

1. This boundary supports the manual BotFather fallback only. It does not claim automatic Telegram bot-account creation.
2. The owner places the BotFather token and an independent webhook secret directly into the reviewed secret store. ClientPlatform receives only `secret://`, `kms://` or `vault://` references.
3. A tenant-scoped durable request transitions through `awaiting_secret`, `ready`, `verifying`, `completed`, `failed` or `cancelled`.
4. `(business_id, provider, idempotency_key)` identifies one request. Repeating request creation returns the existing request.
5. Verification uses a one-time lease token. Only its owner may complete or fail the attempt.
6. A live lease cannot be stolen. A lease older than the bounded timeout may be atomically replaced; the old worker can no longer mutate the request.
7. Telegram `getMe` and `setWebhook` run outside the database transaction. The verified username must match an explicitly requested username when one was supplied.
8. The public webhook URL contains only the numeric bot ID. Neither the bot token nor webhook secret appears in the path.
9. Final creation and activation of `connections` and `managed_bots`, plus completion of the provisioning request, happen in one database transaction.
10. If Telegram configuration succeeds but the database transaction fails, the provisioner performs compensating `deleteWebhook` and records a durable failed attempt.
11. Cancellation is idempotent and removes secret references from the provisioning request.
12. Completed requests are idempotent. Repeating finalization returns the existing connection and managed-bot result without calling Telegram again.
13. The provisioning table is privacy-classified, included in production backup evidence and verified in disposable PostgreSQL restore drills.
14. A future automatic provider must implement the same provision/rollback contract and receive a separate security review.

## Consequences

- Raw BotFather tokens never become ClientPlatform tenant data.
- Double submission cannot create two active managed bots.
- A crashed verifier can be recovered after a bounded lease timeout.
- Telegram and database state converge through a compensating rollback rather than an open distributed transaction.
- The first supported workflow still requires the owner or operator to create the bot in BotFather and store its secrets safely.

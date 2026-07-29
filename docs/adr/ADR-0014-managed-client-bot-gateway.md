# ADR-0014: Tenant-safe Managed Client Bot Gateway

## Status

Accepted — 2026-07-29.

Acceptance evidence was recorded on implementation head `443982955e3a656252a646779ef725a756518602`. All eleven exact-head workflows were green, including the Managed Bot Gateway PostgreSQL 16 matrix, production backup/restore proof, Release Gate, static/security, Canon, user scenarios, booking concurrency and CI coverage ratchets. Coverage was raised and locked at 71.25% combined and 62.48% branch.

## Context

ClientPlatform already stores tenant-scoped external connections, managed-bot metadata, customer identities and a durable outbound dispatch outbox. The production application, however, accepts Telegram updates through one global `Bot` object and one global webhook secret. Merely registering more bot tokens would therefore create ambiguous tenant routing, replay races, shared failure domains and a risk that a customer entering through a specialist's personal bot is routed into owner onboarding.

A process-per-bot design would multiply deployments, database pools, health checks and restart domains. It would also make reliable cross-bot limits and observability difficult. Raw bot tokens must never become tenant database data or callback payloads.

Telegram's ordinary Bot API can configure an existing bot but does not expose an application API that creates a new bot account. Therefore automatic account creation is a separate provider/provisioning concern and must not weaken the gateway boundary.

## Decision

1. One ClientPlatform application process owns one shared HTTP ingress and one Managed Bot Gateway worker.
2. An active Telegram `external_bot_id` maps globally to exactly one `managed_bots` row and one `business_id`.
3. A business has at most one active managed Telegram bot in this foundation. Registration, activation, disabling and revocation share deterministic PostgreSQL advisory-lock namespaces; replacement requires disabling or revoking the previous route first.
4. Bot tokens and webhook secrets remain secret-manager references. Database rows contain only `secret://`, `kms://` or `vault://` references; the current production adapter resolves only reviewed `secret://env/CLIENTPLATFORM_SECRET_*` references.
5. The public route contains the non-secret Telegram bot ID, never a token or webhook secret. Authentication uses Telegram's `X-Telegram-Bot-Api-Secret-Token` header and constant-time comparison.
6. HTTP admission validates the route and secret, bounds JSON size, checks the provider update ID, serializes admission per bot, records the update durably and responds without executing business handlers.
7. `(managed_bot_id, provider_update_id)` is the replay key. An identical repeat is successful and idempotent; reuse with another SHA-256 digest fails closed.
8. PostgreSQL uses transaction-level advisory locks for admission and `FOR UPDATE SKIP LOCKED` for worker claims. SQLite uses `BEGIN IMMEDIATE` as a coarse local-development fallback.
9. Rate and queue limits are scoped per managed bot. Saturation or repeated handler failure for one bot must not block another bot.
10. Worker leases are recoverable after timeout. Each event transitions through `pending`, `processing`, `retry`, `processed` or `dead`.
11. Full payload JSON is removed after `processed` or `dead`; update ID, digest, timestamps and sanitized error code remain as replay/audit evidence. Disabling or revoking a route also terminalizes its queued events and removes their payloads.
12. Before an authenticated managed-bot update enters the dispatcher, ClientPlatform creates or reuses one Telegram customer identity inside the route's business.
13. Dispatcher workflow data carries `managed_bot_business_id`, `managed_bot_id` and `managed_bot_connection_id`. `/start` in a personal bot opens only that specialist's customer portal, ignores foreign invite payloads and never falls back to owner onboarding.
14. Initial program delivery prefers the active managed Telegram connection. Later lessons inherit the previous dispatch route, so the whole program remains on the personal bot.
15. Systemd and Docker production starts run both the production-isolation preflight and the Managed Bot Gateway preflight. Caddy exposes only the tokenless gateway prefix.
16. Automatic creation/configuration of Telegram bot accounts belongs to a later provisioning adapter. BotFather fallback may collect a token only through a direct secret-store channel; ClientPlatform must receive only the resulting reference.

## Consequences

- Thousands of tenant routes can share one bounded application runtime without one process per bot.
- Tenant identity is resolved before handler execution rather than inferred from Telegram user ID.
- Duplicate webhook delivery is safe, while conflicting replay is visible and rejected.
- One bot can enter retry/dead-letter state without stopping the fleet.
- Outbound content follows the same managed connection that represents the business.
- Safe route rotation is available without leaving stale queued payloads or allowing two active bots for one business.
- Operational readiness requires PostgreSQL two-connection evidence, queue/fleet health and tested restore of gateway tables.
- This foundation does not itself create Telegram bot accounts, upload avatars or purchase provider capacity.

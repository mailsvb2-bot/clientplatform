# ADR-0013: Default-on ClientPlatform control bot and tenant-safe booking

## Status

Accepted — 2026-07-28. Hardened — 2026-07-28. Supersedes the default-off rollout statements in ADR-0007, ADR-0008 and ADR-0012. Their architectural boundaries remain valid.

## Context

The first ClientPlatform owner journey was deliberately protected by an opt-in flag while the product UI, tenant boundary and Telegram dispatch path were being proven. That phase is complete: the control router precedes the imported legacy handlers, the dispatch owner has fail-closed readiness, and the complete journey is covered by regression tests.

Consultations, services and custom activity connectors also need a concrete fulfilment path. A free-form offering without availability or customer self-booking still requires manual coordination outside ClientPlatform.

The first booking implementation enforced overlap rules with a read followed by a separate write. Under PostgreSQL concurrency, two transactions could both pass the read before either committed. The same defect existed when one customer concurrently claimed two different overlapping slots. Local wall-clock parsing also attached `ZoneInfo` without proving that the entered time existed or was unambiguous across a DST transition. Finally, a Telegram principal with both business membership and customer links was routed only to the business portal.

## Decision

1. `control_bot_enabled()` returns `True` when `CLIENTPLATFORM_CONTROL_BOT_ENABLED` is absent.
2. `dispatch_runtime_config()` and health/readiness use that same default.
3. `CLIENTPLATFORM_CONTROL_BOT_ENABLED=0` and `CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED=0` are the explicit emergency rollback controls.
4. Unknown boolean values fail fast; they never silently expose the imported legacy interface.
5. Production env and systemd templates declare both defaults as `1` while still allowing environment files to override them.
6. Consultations, services and custom offerings may publish `booking_slots` in the business profile timezone.
7. A connected Telegram customer receives a client portal on repeated `/start`, sees only open future slots for businesses to which that Telegram identity is connected, and books atomically.
8. A slot may be claimed by one customer only. Overlapping slots for one offering and overlapping bookings for one customer are rejected.
9. `booking_slots` is business-scoped, protected by composite tenant foreign keys and included in the fail-closed privacy manifest and runtime schema readiness.
10. PostgreSQL serializes the offering overlap invariant with a transaction-scoped advisory lock keyed by `(business_id, offering_id)`. The customer overlap invariant uses a separate transaction-scoped advisory lock keyed by `(business_id, customer_id)`. Locks are acquired before the relevant read and released automatically on commit or rollback.
11. SQLite local development uses `BEGIN IMMEDIATE` before the same check-and-write sections. This is a deliberately coarser fallback, not a claim that SQLite provides PostgreSQL production concurrency.
12. PostgreSQL CI must exercise each race through two independent connections and prove exactly one successful transition.
13. Local booking input is accepted only when a UTC round trip proves one real wall-clock occurrence. DST gaps and repeated autumn times fail closed instead of being silently shifted or assigned an arbitrary offset.
14. When a Telegram principal has both active business memberships and active customer links, `/start` presents an explicit choice between **«Мои бизнесы»** and **«Мои специалисты и программы»**. Each callback re-resolves live access before opening a portal.

## Consequences

- The next normal application start presents ClientPlatform rather than the imported baseline start interface.
- The dispatch worker and readiness checks are active without an extra rollout flag.
- Operators retain a deterministic two-variable rollback.
- Connected clients no longer fall into owner onboarding when they return to the bot.
- Dual-role users retain both owner and customer journeys without inferred precedence.
- Booking overlap guarantees now hold at the transaction boundary, not only in sequential tests.
- Invalid DST wall-clock values require correction by the owner; ClientPlatform never silently publishes a different appointment time.
- Booking is an extension vertical over generic offerings; future calendar, payment, reminder and rescheduling connectors can reuse the same tenant/customer identity boundary.

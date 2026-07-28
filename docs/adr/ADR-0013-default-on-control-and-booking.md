# ADR-0013: Default-on ClientPlatform control bot and tenant-safe booking

## Status

Accepted — 2026-07-28. Supersedes the default-off rollout statements in ADR-0007, ADR-0008 and ADR-0012. Their architectural boundaries remain valid.

## Context

The first ClientPlatform owner journey was deliberately protected by an opt-in flag while the product UI, tenant boundary and Telegram dispatch path were being proven. That phase is complete: the control router precedes the imported legacy handlers, the dispatch owner has fail-closed readiness, and the complete journey is covered by regression tests.

Consultations, services and custom activity connectors also need a concrete fulfilment path. A free-form offering without availability or customer self-booking still requires manual coordination outside ClientPlatform.

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

## Consequences

- The next normal application start presents ClientPlatform rather than the imported Metrotherapy start interface.
- The dispatch worker and readiness checks are active without an extra rollout flag.
- Operators retain a deterministic two-variable rollback.
- Connected clients no longer fall into owner onboarding when they return to the bot.
- Booking is an extension vertical over generic offerings; future calendar, payment, reminder and rescheduling connectors can reuse the same tenant/customer identity boundary.

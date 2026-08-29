# ADR-0126 — User-facing capability parity

## Status

Accepted for the owner-directed UI/UX capability parity pass.

## Context

ClientPlatform can have a setup ingress available while a concrete provider runtime is disabled or not ready. A UI that checks only the generic setup surface can therefore expose a connection button that cannot lead to a working channel. The same mismatch can appear across Telegram admin and native VK/MAX staff surfaces.

## Decision

One server-resolved application projection combines tenant connection facts with runtime enablement, readiness, and secure setup availability. User-facing surfaces consume that projection instead of reimplementing provider/runtime checks.

For Telegram, VK, and MAX the projection distinguishes:

- working;
- requires attention;
- setup in progress;
- available to connect;
- connected but runtime currently disabled;
- unavailable in the current installation.

A connection action is rendered only when the exact channel is connectable. The target handler/application action revalidates the same projection, so forged or stale callbacks fail closed. Staff switch links are shown only for channels that are currently active.

The owner `Business and capabilities` surface shows the same runtime truth next to business modules. Yandex Direct is projected as a separate external capability when the current role may inspect advertising connections.

Editorial publication planning remains provider-independent. Its UI must say that choosing a platform creates a content-plan entry and does not itself send or publish anything.

## Consequences

- Generic omnichannel setup enablement no longer makes disabled VK/MAX channels look usable.
- Existing connections remain visible when their runtime is disabled, instead of disappearing.
- UI and callback authorization share the same availability facts.
- Provider secrets and raw configuration values remain outside the presentation layer.
- Enabling a channel in production automatically makes the connect action appear once readiness is satisfied; disabling it removes the action without a UI code change.

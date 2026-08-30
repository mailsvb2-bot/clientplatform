# ADR-0126 — User-facing capability parity

## Status

Accepted for the owner-directed UI/UX capability parity pass.

## Context

ClientPlatform has two distinct messenger ingress families: legacy global VK/MAX webhooks and the canonical tenant-scoped omnichannel runtime. User-facing VK/MAX setup belongs to the tenant-scoped runtime, where provider tokens and webhook secrets are verified during onboarding and stored through encrypted credential references. The UI must not use legacy global webhook flags as the availability signal for native ClientPlatform connections.

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

- Canonical omnichannel enablement is necessary but not sufficient for tenant-scoped VK/MAX availability: the dispatch runtime must be enabled and the native security/preflight contour must be ready. Legacy global VK/MAX webhook flags do not gate native ClientPlatform connections.
- Existing connections remain visible when their runtime is disabled, instead of disappearing.
- UI and callback authorization share the same availability facts.
- Provider secrets and raw configuration values remain outside the presentation layer.
- Enabling the canonical omnichannel runtime in production automatically makes native VK/MAX connect actions appear once the HTTPS setup surface is ready; disabling it removes those actions without a UI code change.

# ADR-0133 — ClientPlatform managed event personal join

**Status:** accepted  
**Date:** 2026-09-27  
**Scope:** ClientPlatform only  
**Builds on:** ADR-0132

## Context

ADR-0132 added a provider-neutral conference gateway and explicitly prohibited storing
UCR participant-specific single-use join grants in the shared `EventSession.join_url`.
The canonical public event flow still redirected every registration to a shared HTTPS
room, so ClientPlatform could not yet use its managed UCR conference provider from the
existing webinar wizard and personal registration page.

## Decision

1. ClientPlatform keeps Event/EventSession/Registration as the only business source of
   truth. No UCR business state is copied into ClientPlatform.
2. A managed UCR EventSession is represented by `provider_key=ucr` and
   `join_url=NULL`. The shared join URL remains reserved for link-only providers.
3. The owner wizard may select the managed ClientPlatform/UCR venue only when the
   existing UCR gateway is enabled and
   `CLIENTPLATFORM_UCR_CONFERENCE_INTEGRATION_ID` is configured.
4. The personal public join route first resolves a registered, active capability token
   and the tenant-scoped EventSession from ClientPlatform storage.
5. Network I/O happens after the database read scope is closed; no database transaction
   is held across UCR calls.
6. ClientPlatform uses opaque IDs only:
   - conference external id: `clientplatform:event-session:<session UUID>`;
   - participant external id: `clientplatform:event-registration:<registration UUID>`.
   Name, e-mail and phone are not sent for join routing.
7. Conference create, participant ensure and runtime prepare use stable idempotency keys.
   Each browser join action receives a separate UUID-scoped idempotency key and requests
   a fresh single-use join grant.
8. The returned grant URL is never persisted into EventSession or Event.
9. Immediately before redirect ClientPlatform revalidates that the registration and
   event are still active. A cancellation race therefore fails closed; an already
   issued orphan grant is bounded by UCR TTL and is not exposed to the cancelled user.
10. Provider/gateway/capability failure renders a bounded 503 ClientPlatform page and
    does not fall back to a guessed/shared URL.
11. The existing join-click funnel signal is recorded only after provider join issuance
    succeeds and the registration is revalidated.
12. This ADR changes no UCR repository code and authorizes no production deployment or
    feature-flag activation.

## Consequences

The existing ClientPlatform webinar user path can use a managed conference provider
without introducing a second webinar domain or leaking a participant grant through
shared event state. External-link providers retain their existing behavior.

Production use still requires the separately managed UCR gateway/listener, service
principal permissions, integration id, realtime capability proof and explicit owner
deployment/activation.

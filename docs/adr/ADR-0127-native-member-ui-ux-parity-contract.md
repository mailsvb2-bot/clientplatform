# ADR-0127 — Native member UI/UX parity contract

## Status

Accepted for the owner-directed Telegram/VK/MAX UX parity pass.

## Context

ClientPlatform exposes staff and owner workflows through Telegram and through the native VK/MAX member interaction layer. Before this change, the native layer covered the main navigation and many reads, but several Telegram actions either had no native equivalent or rendered simplified placeholder facts. That made the product depend on which messenger the employee happened to use.

Pixel-identical layouts are neither possible nor desirable because Telegram, VK and MAX have different interaction primitives and transport limits. Product parity instead means that the same role can reach the same user intent, see the same canonical business facts, and perform the same canonical mutation with the same authorization and safety rules.

## Decision

1. Telegram, VK and MAX share canonical application/domain operations. Messenger code is only an interaction adapter.
2. Native VK and MAX continue to share one member renderer and parser.
3. Every reachable Telegram admin action must have a documented native semantic equivalent, except pure transport navigation such as back/return callbacks.
4. Native reads for payments, prices, publications, automation approvals and growth use the same canonical application projections as Telegram rather than placeholder values.
5. Native writes for publications, payments/refunds, prices, AutomationPolicy decisions, customer invites, capabilities, team membership, business activity, programs, lessons, program delivery and offerings use the existing canonical application APIs and their RBAC/idempotency rules.
6. Text-entry workflows may use a single validated native command instead of Telegram FSM state. This is transport adaptation, not a product capability difference.
7. The native transport button ceiling is handled with progressive disclosure; features may not be silently dropped to fit one screen.
8. Stale, forged or unauthorized native mutation commands fail closed and do not weaken canonical role boundaries.
9. Customer invite codes are channel-neutral and may be claimed through Telegram, VK or MAX ingress.
10. Tests source-check reachable Telegram admin actions against the semantic-equivalence registry so future Telegram-only product actions fail CI until a native equivalent is added.
11. Common owner intents from the short Telegram experience are also registered explicitly: clients, programs, booking, result, customer invite, offerings and advanced controls. Program creation, lesson creation, publication and delivery are real native workflows rather than read-only placeholders.
12. Native program delivery is channel-exact: a VK staff action uses the active VK business connection and a MAX staff action uses the active MAX connection. The chooser exposes only customers with an active identity in that channel and fails closed instead of silently switching providers.
13. Provider-event retries may not duplicate native create mutations. Program, lesson, publication and offering creation bind one provider interaction key to one deterministic canonical entity; replay returns the same entity, while reuse of the key with different work fails closed. Native Autopilot controls set an explicit desired state instead of replay-unsafe toggling.
14. Money parity uses the canonical ISO-4217 minor-unit exponent for input and display in every messenger. Business-wide payment counts, paying-customer counts and revenue totals come from untruncated canonical aggregates; limited payment queries are used only for recent-row lists and action buttons.

## Consequences

- Owners and staff can use the same ClientPlatform business workflows from Telegram, VK or MAX when that messenger runtime is enabled.
- Wording and button arrangement may remain messenger-native, while user intent, facts, mutations, permissions and safety semantics remain equivalent.
- A new Telegram admin capability can no longer be treated as complete if the native parity contract is not updated and tested.
- This ADR does not enable a disabled provider runtime and does not itself deploy or change production configuration.

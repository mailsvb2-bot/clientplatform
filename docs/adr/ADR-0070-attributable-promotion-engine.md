# ADR-0070: Attributable Promotion Engine without a BusinesAIOS runtime dependency

- Status: Accepted
- Date: 2026-08-05
- Scope: ClientPlatform promotion copy, source links and booking attribution

## Context

ClientPlatform already owns the lower conversion path: business offering, published
availability, public booking page, atomic booking and reminders. A published slot
was still commercially passive because the owner received only a generic share
link and could not distinguish which channel produced a visit or booking.

BusinesAIOS contains mature ideas for provider-neutral creative generation,
content guardrails, stable variant identifiers and outcome attribution. Importing
its DecisionCore or autonomous growth runtime would change ClientPlatform into a
different product and create an unacceptable operational dependency.

## Decision

ClientPlatform receives a narrow, native Promotion Engine implemented against its
existing domain boundaries. The implementation adapts only these ideas:

- deterministic creative candidates and stable creative identifiers;
- conservative content guardrails and safe fallback copy;
- one durable source token for each `business × slot × channel` campaign;
- idempotent evidence for unique campaign opens and attributed bookings;
- result reporting in the owner journey.

The copied concepts are rewritten as ClientPlatform domain, application,
infrastructure and Telegram modules. BusinesAIOS is not imported at runtime and
is not a package, network or database dependency.

## Product boundary

ClientPlatform remains responsible for:

- offers and services;
- availability and calendar;
- public customer entry;
- booking and reminders;
- promotion materials for existing offers;
- source attribution through booking.

The first version does not:

- choose or spend an advertising budget;
- autonomously optimize campaigns;
- connect external ad accounts;
- publish through VK, WhatsApp or advertising APIs;
- import DecisionCore, MarketGraph or the BusinesAIOS growth runtime.

Telegram uses the provider's standard share action. Other channels receive safe
copy and a channel-specific attributable link. Provider-native publishing must be
added separately with OAuth/secret custody, budget limits, rate limits, audit and
rollback evidence.

## Data and privacy

`promotion_campaigns` stores business-owned copy, channel, slot relation and a
random public capability token. `promotion_events` stores only tenant customer
IDs and the bounded outcome type `opened` or `booked`; it stores no raw Telegram
ID, message body, IP address or browser fingerprint.

Opening is deduplicated per campaign and customer. Booking reuses the canonical
BookingRepository transaction and writes attribution in the same transaction.
A campaign link becomes unavailable when its slot is no longer open.

## Roles

Owners, administrators, managers, content managers and marketers can create
promotion material. Those roles plus analysts can read promotion analytics.
Support cannot create campaigns or read acquisition analytics.

## Consequences

A specialist can now move from `published availability` to `channel-specific
advertisement` to `measured booking` without leaving ClientPlatform. The system
still cannot claim paid reach, impressions, advertising cost or revenue until a
reviewed provider and payment evidence source exists.

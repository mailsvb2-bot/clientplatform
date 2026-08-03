# ADR-0066: Result-first onboarding, external cloud media and booking reminders

**Status:** accepted
**Date:** 2026-08-04

## Context

The inherited Telegram administration surface exposes many correct but advanced operations at once. That is useful for an experienced operator, but it makes the first contact intimidating for a non-technical specialist. The same problem existed in program media: the visible path encouraged uploading files into ClientPlatform storage even when the owner already kept large audio and video files in a cloud service. Booking confirmation also remained only a chat message and did not give the phone a calendar event or durable reminders.

## Decision

1. The first contact explains the result in ordinary language and offers one primary action: **«Запустить мой бизнес»**.
2. After the owner gives a business name and activity description, ClientPlatform enables the safe baseline capabilities automatically. The result-first dashboard has one recommended next action and four common outcomes. The complete administration surface remains available through **«Все возможности»**.
3. Program material creation is cloud-first. The owner first chooses the material type and then its location. Public links from Yandex Disk, Google Drive, Dropbox and OneDrive are stored as tenant-scoped lesson references; the media bytes are not copied to ClientPlatform. Streaming pages are delivered as links rather than incorrectly pretending to be downloadable video files. Small device uploads remain available through the existing private S3 boundary.
4. Draft lessons visibly expose open, replace, rename and delete actions. Replacing a private object with an external link queues best-effort cleanup of the superseded private object.
5. A confirmed booking creates two independent reminder layers:
   - a standards-compliant `.ics` event with alarms at 24 hours and 1 hour, which the user can import into the phone calendar;
   - persistent Telegram reminder jobs at the same offsets. Every reminder rechecks that the customer still owns an active booking before sending.

## Platform limitation handled explicitly

A Telegram bot cannot silently create an operating-system alarm or open a native file picker without a user gesture and platform permission. ClientPlatform therefore sends an importable calendar file and a Google Calendar action, while Telegram reminders provide the server-side fallback. Cloud links use the provider's public-share flow; a future Mini App with provider OAuth can add a native picker without changing lesson storage contracts.

## Security and economics

- only HTTPS public links are accepted;
- credentials embedded in URLs, localhost, private IP addresses and non-standard ports are rejected;
- Yandex download resolution calls only the fixed public Yandex API endpoint;
- private S3 references continue to use signed short-lived gateway URLs;
- external media does not consume ClientPlatform object storage or outbound upload bandwidth;
- reminder jobs are idempotent and customer-scoped.

## Verification

Required regression coverage includes URL validation/provider conversion, calendar alarms, reminder idempotency, customer booking ownership, cloud add/replace flows, simple onboarding and the existing full ClientPlatform scenario matrix.

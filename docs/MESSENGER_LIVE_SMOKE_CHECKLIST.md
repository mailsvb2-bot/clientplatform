# ClientPlatform live messenger E2E checklist

This checklist is the human-readable mirror of
`config/live_e2e_manifest.json`. The executable manifest and its validator are
the source of truth for live coverage.

## Safety boundary

Live E2E uses only a dedicated ClientPlatform staging tenant and dedicated test
accounts. Personal accounts, production credentials, production customer data,
real payment methods and credentials from other products are forbidden.

The Windows runner must be interactive. A GitHub Actions runner installed as a
Windows service cannot drive Telegram Desktop or MAX Desktop reliably and is
therefore not accepted by the preflight.

## Surfaces

- Telegram Desktop: owner/member/customer staging session.
- VK: dedicated test account in Chrome or Edge.
- MAX Desktop: dedicated test account.
- ClientPlatform cockpit: both Edge and Chrome.
- Server side: the same staging tenant, with synthetic business/customer data.

## Canonical journeys

1. Owner entry, business creation/selection and explicit active-business context.
2. Today/home, customers, sales, connections and system/status surfaces.
3. Telegram/VK/MAX connection status and channel switching.
4. Growth, acquisition, funnels, segments, retention and AutomationPolicy.
5. Publications: draft, schedule, cancel, publish and duplicate-event replay.
6. Programs: create draft, lessons, publish, customer delivery, media and progress.
7. Booking: slot creation, date/time/duration selection, customer booking and concurrency.
8. Offerings, business profile, activity directions and enabled formats.
9. Money/prices/tariff branches using synthetic evidence or provider sandbox only.
10. Team/RBAC: member invite/change/revoke plus immediate revoked-access denial.
11. Events and event editing/wizard flows.
12. Customer invite/identity claim through Telegram, VK and MAX.
13. Creative Studio image/video through a zero-cost test project or sandbox only,
    including visible result and download action.
14. Cross-channel parity: the same semantic action must be accepted in Telegram,
    VK and MAX without creating a second business-logic implementation.
15. Resilience: duplicate ingress, restart recovery and temporary provider failure.
16. Cross-tenant isolation and forged/stale action rejection with two synthetic businesses.
17. Platform-operator read-only/support flows in a separate high-trust staging account.

## Evidence

A successful run keeps only synthetic evidence for seven days:

- per-live-transport/channel screenshots from the dedicated VM;
- UI accessibility-tree fingerprints, not raw credentials;
- action counts and pass/fail status;
- the combined `live-e2e-report.json`.

The runner must not upload browser profiles, Telegram/MAX session databases,
cookies, tokens, environment dumps or arbitrary home-directory files.

## Stop condition

The live contour is green only when the hermetic user-scenario wall passes first
and every configured live probe succeeds. A provider outage is a failed live
run, not a reason to silently substitute mocks. Paid branches must remain
sandbox-only; absence of a sandbox is reported as blocked rather than spending
real money.

# ADR-0130 — Protected Windows 10 + Windows 11 live E2E contour

## Status

Accepted for the owner-directed live E2E work. The repository implementation can
be merged before physical runners are connected, but live readiness is not
claimed until exact-main runs on both Windows versions produce evidence.

## Context

ClientPlatform already has broad hermetic regression coverage and a User
Scenario Matrix, but those jobs run on Linux and intentionally do not use live
provider sessions. They cannot prove that a real Telegram/VK/MAX user can reach
the same canonical behavior through installed clients and browsers.

The previous messenger live checklist also described imported, non-ClientPlatform
consumer flows. Keeping that document as evidence would allow a false positive.

A single Windows runner is not sufficient evidence for user compatibility across
Windows 10 and Windows 11. The operating systems must be independently routed,
validated and reported.

## Decision

ClientPlatform has two complementary E2E layers:

1. The existing hermetic scenario wall remains the exhaustive deterministic
   regression layer.
2. A protected two-leg Windows x64 self-hosted contour exercises dedicated
   staging user sessions on both Windows 10 and Windows 11 through Telegram
   Desktop, MAX Desktop, VK in a browser, and the cockpit in both Edge and
   Chrome. The two OS legs use distinct runner labels and produce separate
   evidence; one operating system can never satisfy the other operating
   system's live gate.

The live contour is repository-owned by
`config/live_e2e_manifest.json`. CI compares that manifest with the canonical
Telegram-to-native VK/MAX semantic parity registry. A new native user action
therefore cannot silently escape the live coverage declaration. The same
manifest also owns the accepted Windows variants and minimum builds.

The self-hosted jobs never run for pull requests. They check out trusted
`main`, use a dedicated GitHub Environment, require an interactive desktop
session, and fail closed unless the environment is explicitly staging with
dedicated test accounts. Production credentials and real money are rejected.

Before the Python live orchestrator runs, PowerShell validates the actual
Win32 operating-system caption/build and writes a non-secret runner identity
record. The orchestrator requires that identity to match its matrix leg.

Desktop sessions are stored only in the dedicated Windows test profiles. They
are not exported to GitHub secrets or artifacts. Evidence contains synthetic
screenshots and non-secret accessibility fingerprints only.

Paid payment/AI branches use provider sandbox or a deliberately zero-cost test
project. If no safe provider contour exists, the scenario is BLOCKED-LIVE; the
test does not spend real money to turn green.

## Evidence model

The suite intentionally reports two different facts and never merges them into
one percentage:

- **semantic contract coverage** — every canonical Telegram/native action is
  represented in the repository manifest and exercised by deterministic
  application/contract tests;
- **live transport evidence** — only steps actually performed through the
  dedicated Telegram/VK/MAX/browser sessions count, and each such step must
  make a new expected user-visible text appear.

Live transport evidence is additionally partitioned by `windows-10` and
`windows-11`. Overall Windows compatibility requires both OS-specific matrix
legs to pass.

A provider response, screen change, or callback acknowledgement without the
expected semantic text is a failed live probe. This prevents a generic error,
permission warning or help prompt from being counted as success.

Mutation-heavy live journeys require a deterministic staging fixture/reset.
Until that fixture exists and the explicit mutation steps are added, the live
report must not claim those mutations were executed. Hermetic coverage remains
mandatory and cannot be presented as live evidence.

## Consequences

- Hermetic tests cannot be replaced by live UI automation, and live UI evidence
  cannot be replaced by mocks.
- Windows 10 and Windows 11 each require a correctly identified x64 live runner;
  overall Windows compatibility is not green unless both matrix legs pass.
- A self-hosted runner must run interactively, not as a Windows service.
- Untrusted PR code never executes on the credential-bearing desktop runners.
- Telegram/VK/MAX semantic drift is a CI failure at the manifest boundary.
- Provider/client changes can break live E2E even when unit tests are green; that
  is useful evidence, not flakiness to suppress.
- Actual production readiness still requires the normal ClientPlatform release
  gates and, where relevant, a separately authorized production deploy/probe.

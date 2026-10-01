# ADR-0131 — Real-device mobile and tablet live E2E

## Status

Accepted for the owner-directed cross-device live E2E work. Repository support
does not by itself prove device compatibility; exact-main evidence from each
physical target is required.

## Context

Desktop Windows coverage cannot prove the behavior of native messenger clients,
mobile browsers, touch layouts or provider SDK behavior on phones and tablets.
Likewise, an Android phone does not prove an Android tablet, and iPhone does not
prove iPad.

Emulators are useful development tools but cannot be the only release evidence
for persistent third-party messenger sessions. ClientPlatform therefore needs a
real-device compatibility layer without exposing production credentials to a
public-repository runner.

## Decision

The canonical live matrix adds eight independent device targets:

1. Android phone;
2. Android tablet;
3. iPhone;
4. iPad;
5. HarmonyOS phone;
6. HarmonyOS tablet;
7. Fire OS tablet;
8. ChromeOS tablet/convertible.

Android-family targets use ADB identity plus Appium UiAutomator2. Apple targets
use the Xcode real-device inventory plus Appium XCUITest. HarmonyOS uses HDC and
DevEco Testing Hypium through a strict evidence adapter.

Every target is routed by a unique self-hosted runner label. The runtime
validator independently checks the connected target so a mislabeled runner
cannot silently satisfy another matrix leg.

## Evidence model

The existing semantic registry remains hermetic application evidence. Mobile
live evidence is a separate class and is partitioned by target ID.

A provider probe passes only if a new expected response is visible and the UI
fingerprint changes. Raw accessibility/UI trees are not retained. Screenshots
contain synthetic test sessions only.

HarmonyOS evidence is accepted only when the runner-local Hypium executor
returns the exact probe set, marks expected text as asserted, and supplies a
screenshot for every probe. Exit code zero alone is insufficient.

## Security boundary

The credential-bearing workflow never runs pull-request code and checks out
trusted `main`. Device profiles hold dedicated test sessions only. Repository
profiles contain locators/app IDs/URLs, never passwords, tokens, cookies or
authorization material.

Production credentials and real-money execution fail closed.

## Consequences

- Overall mobile compatibility cannot be called green until every required
  target has exact-main live evidence.
- Missing provider availability on a target is BLOCKED-LIVE, not a synthetic
  PASS and not silently replaced by another platform.
- Device/host automation dependencies are provisioned outside the repository;
  the repository owns the validation contract, execution plan and evidence
  acceptance rules.
- Form-factor-specific scenarios such as rotation, split-screen, suspend/resume
  and ChromeOS touchview transitions can be added as explicit probes without
  weakening the transport acceptance evidence.

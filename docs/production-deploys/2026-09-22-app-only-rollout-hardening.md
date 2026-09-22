# Production rollout request after app-only rollout hardening

Date: 2026-09-22

Validated main SHA for rollout: `52bff520097d529d2cb77a7e0f23dcf018c9d69d`.

Scope: deploy the current protected `main` to the dedicated ClientPlatform production host after PR #407 hardened the rollout path for proven application-only changes. This release includes the already-merged omnichannel parity work through activity directions, webinar schedule editing, booking wizard parity and program-draft editor parity, plus the production disk-maintenance safeguards already merged to `main`.

Pre-deploy evidence:

- PR #407 exact head `9c3ac1afbac0616520a8623a71d8095911d9521d` completed the required GitHub checks successfully;
- CI regression contour, coverage ratchet and branch coverage ratchet passed;
- Canon, Pre-deploy Release Gate, User Scenario Matrix, Capability Parity, Production Isolation, Encrypted Backup, Boundary Diagnostics, Brand Gate, Critical Static Surface, Booking Concurrency and UCR Gateway Sidecar passed;
- the rollout classifier remains fail-closed: only diffs proven to be limited to `clientplatform/` plus host-only paths may use `app_only`; visual gateway, compose, Dockerfile, requirements, unknown or mixed runtime paths fall back to `full_runtime`;
- app-only rollout does not rebuild or retag the visual gateway and keeps the existing full-runtime disk-capacity thresholds;
- production disk cleanup remains marker-gated and limited to rebuildable builder cache, bounded journal vacuum and apt cache; runtime state and volumes are not pruned.

Transport note: production deployment uses the repository's marker-gated `production-deploy-recovery.yml`. This carrier changes documentation only.

Post-deploy acceptance must confirm the dedicated production checkout reaches the release-carrier SHA, `ops/clientplatform-production-deploy-recovery` reports success, the running application is healthy, and the public ClientPlatform endpoint remains healthy before rollout is considered complete.

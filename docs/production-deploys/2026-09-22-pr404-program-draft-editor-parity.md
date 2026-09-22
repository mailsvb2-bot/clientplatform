# PR #404 production rollout request

Date: 2026-09-22

Application merge SHA validated for rollout: `5e349dabc570c63feacbbc0ea7372172a3cff8db`.

Scope: deploy the already-merged ClientPlatform program-draft editor parity from PR #404 to the dedicated ClientPlatform production host. The release gives native VK and MAX staff the same canonical draft-editor intents already available in Telegram: reopen saved drafts, browse lessons, rename lessons, replace lesson material/type, reorder lessons, delete lessons, delete drafts, continue authoring and publish.

Pre-deploy evidence:

- protected `main` merge completed through PR #404;
- exact PR head `738e048b65b313c1b97786892d65bbc29a220542` completed the required GitHub checks successfully;
- Canon, Pre-deploy Release Gate, User Scenario Matrix, Capability Parity, Production Isolation, Encrypted Backup, Boundary Diagnostics, Brand Gate, Critical Static Surface, Booking Concurrency and UCR Gateway Sidecar passed;
- CI regression contour, coverage ratchet and branch coverage ratchet passed;
- local targeted omnichannel/parity proof completed 173 tests successfully;
- local canonical `scripts/regression_gate_ci.py` completed with `REGRESSION_GATE_OK`;
- all native mutations delegate to existing canonical program-draft application boundaries; no channel-specific program storage is introduced.

Transport note: production deployment uses the repository's marker-gated `production-deploy-recovery.yml`. This release carrier changes documentation only; it does not modify application logic, schema, credentials, business rules or infrastructure topology.

Post-deploy acceptance must confirm the dedicated production checkout reaches this release carrier SHA, the protected recovery status reports success, the running application is recreated successfully, and the public ClientPlatform endpoint remains healthy before rollout is considered complete.

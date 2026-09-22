# PR #400 production rollout request

Date: 2026-09-22

Application merge SHA validated for rollout: `ba6609390d771b49223edfa98e078bc152474674`.

Scope: deploy the already-merged ClientPlatform omnichannel webinar schedule-edit parity from PR #400 to the dedicated ClientPlatform production host. The release exposes webinar schedule editing through native VK and MAX alongside Telegram while keeping one canonical event/session state.

Pre-deploy evidence:

- protected `main` merge completed through PR #400;
- exact PR head `01f07d40fe138af9feb9eb02fe50aabe039c3113` completed the required GitHub checks successfully;
- both PR and push `quality / py3.12` contexts passed, including regression contour, coverage ratchet and branch coverage ratchet;
- Pre-deploy Release Gate, Canon, User Scenario Matrix, Capability Parity, Production Isolation, Encrypted Backup, Boundary Diagnostics, Brand Gate, Critical Static Surface and UCR Gateway Sidecar passed;
- local targeted omnichannel/parity suite completed 160 tests successfully;
- canonical `scripts/regression_gate_ci.py` completed with `REGRESSION_GATE_OK`;
- native schedule editing preserves existing room/provider metadata and uses `configure_event_sessions(..., reschedule_notifications=True)`;
- rollout is authorized by the owner in the active ChatGPT session.

Transport note: production deployment uses the repository's marker-gated `production-deploy-recovery.yml`. This release carrier changes documentation only; it does not modify application logic, schema, credentials, business rules or infrastructure topology.

Post-deploy acceptance must confirm the dedicated production checkout reaches this release carrier SHA, deployment evidence reports `CLIENTPLATFORM_PRODUCTION_DEPLOY_OK`, and the public ClientPlatform endpoint remains healthy before rollout is considered complete.

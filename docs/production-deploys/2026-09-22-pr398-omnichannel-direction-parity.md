# PR #398 production rollout request

Date: 2026-09-22

Application merge SHA validated for rollout: `1cde9a454f62b94497dc7bb4886ecd9430ba6702`.

Scope: deploy the already-merged ClientPlatform omnichannel activity-direction parity from PR #398 to the dedicated ClientPlatform production host. The release makes organization activity directions available through native VK and MAX alongside Telegram, and carries the selected direction into canonical program, offering and webinar creation while preserving the explicit “Без направления” path.

Pre-deploy evidence:

- protected `main` merge completed through PR #398;
- exact PR head `69ed9609f9ed51d9b7818a795da9f657e94cfa51` completed the required GitHub checks successfully;
- CI regression contour, coverage ratchet and branch coverage ratchet passed;
- Pre-deploy Release Gate, Canon, User Scenario Matrix, Capability Parity, Production Isolation, Encrypted Backup, Boundary Diagnostics, Brand Gate, Critical Static Surface and UCR Gateway Sidecar passed;
- durable VK/MAX owner-input sessions for direction create/edit are explicitly covered after the final review fix;
- rollout is authorized by the owner in the active ChatGPT session.

Transport note: production deployment uses the repository's marker-gated `production-deploy-recovery.yml`. This release carrier changes documentation only; it does not modify application logic, schema, credentials, business rules or infrastructure topology.

Post-deploy acceptance must confirm the dedicated production checkout reaches this release carrier SHA, deployment evidence reports `ok=true`, and the production application remains healthy before rollout is considered complete.

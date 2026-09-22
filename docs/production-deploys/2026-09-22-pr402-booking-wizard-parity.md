# PR #402 production rollout request

Date: 2026-09-22

Application merge SHA validated for rollout: `2f49597e004e09f590ee852c13a0793fe1933422`.

Scope: deploy the already-merged ClientPlatform omnichannel booking-wizard parity from PR #402 to the dedicated ClientPlatform production host. The release makes booking creation button-first in native VK and MAX alongside Telegram while preserving one canonical booking state and canonical `create_booking_slot` boundary.

Pre-deploy evidence:

- protected `main` merge completed through PR #402;
- exact PR head `619a7e2314b5a5c679b7b062e30691f5ce0878c0` completed the required GitHub checks successfully;
- Canon, Pre-deploy Release Gate, User Scenario Matrix, Capability Parity, Production Isolation, Encrypted Backup, Boundary Diagnostics, Brand Gate, Critical Static Surface, Booking Concurrency and UCR Gateway Sidecar passed;
- CI regression contour, coverage ratchet and branch coverage ratchet passed;
- the Canon-only dependency-light regression was corrected by keeping the Telegram parity proof AST-based instead of importing the aiogram runtime;
- owner authorized continuing the active parity work through merge and production rollout in the current ChatGPT session.

Transport note: production deployment uses the repository's marker-gated `production-deploy-recovery.yml`. This release carrier changes documentation only; it does not modify application logic, schema, credentials, business rules or infrastructure topology.

Post-deploy acceptance must confirm the dedicated production checkout reaches this release carrier SHA, deployment evidence reports `CLIENTPLATFORM_PRODUCTION_DEPLOY_OK`, the running application was recreated successfully, and the public ClientPlatform endpoint remains healthy before rollout is considered complete.

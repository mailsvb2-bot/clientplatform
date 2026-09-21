# PR #392 production rollout request

Date: 2026-09-21

Application merge SHA validated for rollout: `5325c0449da71672d245091365730f8406c99d16`.

Scope: deploy the already-merged ClientPlatform business lifecycle UX from PR #392 to the dedicated ClientPlatform production host. The release adds owner-facing business removal and direction editing across Telegram, VK and MAX, while preserving history and keeping business archival owner-only.

Pre-deploy evidence:

- protected `main` merge completed through PR #392;
- exact PR head `5f1270ed00c1a271fe93d955a757f2a15aa780b0` completed 36/36 GitHub check-runs with no pending or failed checks;
- Pre-deploy Release Gate passed full regression, type contracts, Bandit, dependency audit and release hygiene;
- CI passed static security, PostgreSQL payment/concurrency and the strengthened coverage ratchets;
- coverage baselines were raised to 84.00% total and 76.11% branch coverage;
- User Scenario Matrix, Capability Parity, Canon, Boundary Diagnostics, Production Isolation, Encrypted Backup and UCR Sidecar all passed;
- rollout is authorized by the owner in the active ChatGPT session.

Transport note: production deployment uses the repository's marker-gated `production-deploy-recovery.yml`. The release carrier does not change application logic, schema, credentials, business rules or infrastructure topology.

Post-deploy acceptance must confirm the dedicated production checkout reaches this release carrier SHA, deployment evidence reports `ok=true`, and the production application remains healthy before rollout is considered complete.

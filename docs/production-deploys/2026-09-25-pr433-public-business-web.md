# ClientPlatform production rollout — PR #433 and PR #434

Date: 2026-09-25

Application source before this release carrier: `8646e3fcda0185a436de38b3544af3b0740f5ece`.
Last successful production deploy: `d5dddefbab6c611375bf1f78003f3e8218909fb4` (PR #431).

Scope since that deploy:
- PR #432: business connection structure and owner navigation wording;
- PR #434: missing `sqlite3` imports in the event missing-table guards, plus whole-repository Ruff coverage;
- PR #433: hosted public business web entry at `/clientplatform/b/<token>`.

No schema migration. Rollout is `full_runtime` because `scripts/check_ruff.py` changed.

Pre-merge evidence:
- PR #433 and PR #434 merged to `main` with required checks green;
- `main` at `8646e3fc` reports combined coverage 84.17% / 84.17% and branch coverage 76.33% / 76.33%;
- canonical production deploy remains responsible for the pre-deploy backup, readiness, external HTTPS checks and rollback.

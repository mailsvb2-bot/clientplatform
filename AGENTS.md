# ClientPlatform agent execution protocol

This file is the mandatory startup and execution protocol for every AI chat/agent working in this repository.

## Source-of-truth order

1. `docs/CLIENTPLATFORM_CANON_TZ.md` — the sole normative product and architecture canon.
2. `docs/CLIENTPLATFORM_UNICORN_ROADMAP.md` — the mandatory execution roadmap: priority, sequencing, slices, acceptance criteria and evidence.
3. ADRs in `docs/adr/` — accepted architectural decisions for specific changes.
4. Current `main` code, database contracts and green CI — implementation facts.
5. README and other docs — explanatory surfaces; they must not override the Canon.

If these disagree, use the higher-priority source and fix the lower-priority stale source as part of the same relevant change.

## Mandatory startup sequence

Before editing code:

1. Fetch current `main` and record its exact SHA.
2. Check open PRs and current/failed GitHub Actions so work is not duplicated.
3. Read `docs/CLIENTPLATFORM_CANON_TZ.md` fully.
4. Read `docs/CLIENTPLATFORM_UNICORN_ROADMAP.md` fully.
5. Inspect the current code around the selected slice; never treat an old report or roadmap status as proof that code exists.
6. If the owner gave an explicit task, do that task. Otherwise continue the single highest-priority incomplete `NEXT` roadmap slice.

## Execution rules

- Finish the current slice before starting a different initiative.
- Prefer one coherent vertical slice and one PR over a branch zoo.
- Start from the latest `main`; do not revive stale branches unless they contain verified unique value.
- Find the root cause. Do not patch symptoms or weaken validation/tests for green CI.
- Before creating a new domain/application/infrastructure module, search current `main` for an equivalent canonical module and extend it instead of creating a parallel second implementation.
- Preserve existing user functionality unless the Canon or an explicit owner decision replaces it.
- Keep product logic inside ClientPlatform. External providers and LLMs are adapters, not a second brain.
- Durable business state, money, permissions, attribution, automation policy and decisions belong in explicit domain models and durable storage, not chat memory or prompt text.
- Every tenant-sensitive read/write/job/callback must be business-scoped and server-authorized.
- Every external write must have an idempotency/concurrency strategy and a fail-closed recovery path.
- Advertising spend, paid AI usage and other money-affecting mutations require the canonical consent/policy boundary. Never infer consent from the existence of a draft, campaign or balance.
- Store money as integer minor units plus explicit currency. Never silently add mixed/unknown currencies.
- Do not put real credentials, `.env`, DSN, tokens, private keys, dumps or personal data in GitHub, CI logs, prompts or artifacts.
- Do not use Metrotherapy, BusinessAIOS or another product as a production runtime dependency unless the owner explicitly approves a separate architectural decision.
- Do not production-deploy without a separate direct owner instruction.

## Independent AI review discipline

- AI review is evidence, never a competing source of truth. It cannot override the Canon, deterministic tests, security gates or release evidence.
- One branch has one active writer. Independent reviewers are read-only for that branch; they report findings instead of silently rewriting the author's implementation.
- Every blocking AI review is bound to the exact PR base SHA and head SHA. Any new head commit invalidates the prior PASS and requires a fresh review.
- The trusted `main` version of this protocol governs review of proposed changes to `AGENTS.md`, the Canon, review code and workflows; a PR cannot weaken the rules that judge itself.
- A reviewer may block only on a demonstrated critical/high defect with concrete repository evidence and a reproducible scenario or precise proof path. Style preference is not a blocker.
- Reviewer disagreement is resolved by evidence, not model voting. Confirmed defects should become deterministic regression tests or explicit invariants whenever technically possible.
- No model may infer production readiness from repository inspection. Production readiness still requires the canonical deterministic gates and exact deployed-SHA evidence.
- `AI Review / gate` is a stable head-SHA status whose underlying reviewer evidence is bound to exact base+head SHAs. Enabling it as a required check must also require the PR branch to be up to date with `main`; do not use this status as a merge-queue requirement until an explicit `merge_group`-compatible wrapper exists.
- AI reviewer API keys are CI-only secrets. Untrusted PR code must never execute in a job that can read those secrets.
- Independent AI review is cost-bounded: exact base+head reviews are deduplicated, provider usage is recorded in short-lived CI cost-ledger artifacts, and the gate fails closed before another paid call when its configured monthly or per-review budget would be exceeded.
- The CI budget guard is a repository-side safety layer, not an absolute billing guarantee. Provider-side account/project spend limits should also be configured whenever an absolute monetary ceiling is required.

## Definition of DONE for a roadmap slice

A slice is `DONE` only when all applicable items are true:

1. Domain invariants and failure semantics are explicit.
2. Database schema/constraints/indexes and migrations are additive/safe where needed.
3. Tenant authorization is enforced server-side.
4. Application orchestration and provider boundaries are wired end-to-end.
5. Presentation/UX exposes a result, not provider internals.
6. Idempotency, concurrency, restart and ambiguous-provider-result cases are handled where relevant.
7. Audit/observability and privacy/erasure contracts are updated where relevant.
8. Regression tests cover happy path, fail-closed path and cross-tenant isolation; concurrency/restart tests are added where relevant.
9. Existing CI/security/coverage gates remain green; coverage baselines are never lowered to make a PR pass.
10. The PR is merged into `main`.
11. The roadmap records evidence: PR number, merge SHA and meaningful test/gate result.

Code that exists only in a branch is not DONE.

## Roadmap status discipline

Use these statuses in `docs/CLIENTPLATFORM_UNICORN_ROADMAP.md`:

- `DONE` — merged into `main` with evidence.
- `NEXT` — the one default slice to execute next.
- `QUEUED` — ordered work after NEXT.
- `PLANNED` — important but not yet in the immediate queue.
- `BLOCKED-LIVE` — code can be complete but an external live-provider/production verification is still required.
- `DEFERRED` — intentionally postponed with a reason.

There should normally be only one `NEXT`. Parallel work is allowed only when the owner explicitly asks for it or slices are demonstrably independent and do not create competing implementations.

## Evidence over assertions

When updating the roadmap or reporting completion, cite current repository evidence: merged PR, exact `main` SHA, tests/checks and, for live behavior, the exact environment/probe that was actually exercised. Never describe unverified production behavior as proven.

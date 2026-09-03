# Repository Merge Governance

This document records the repository-side contract enforced by GitHub for `main`.
GitHub branch protection is the enforcement authority; this file is the audited human-readable contract.

## Protected branch

`main` is protected for everyone, including repository administrators.
Normal changes reach `main` only through a pull request.

The protection contract is:

- required status checks use strict/up-to-date semantics;
- pull requests are required, with zero mandatory human approvals for the single-owner repository;
- stale review conversations must be resolved before merge;
- force pushes are disabled;
- branch deletion is disabled;
- administrators are subject to the same protection;
- there is no standing break-glass bypass actor.

## Required stable checks

The following stable check contexts are required on the current pull-request head:

1. `ci/regression-contour`
2. `ci/coverage-ratchet`
3. `ci/branch-coverage-ratchet`
4. `quality / py3.12`
5. `postgres / payment and concurrency`
6. `static security / py3.12`
7. `canon`
8. `brand-gate`
9. `capability-parity`
10. `production-isolation`
11. `release-gate`

GitHub binds these contexts to the GitHub Actions app. Ephemeral workflow run IDs are not part of the contract.
External AI review is useful evidence but is intentionally not a required merge context because external quota or provider availability must not deadlock the repository.

## Merge procedure

Before merging an important pull request:

1. Verify the pull request head SHA has not moved.
2. Verify the branch is up to date with the protected base through GitHub's strict required-check policy.
3. Verify all 11 required contexts are successful on that head.
4. Verify there are no unresolved review conversations.
5. Merge the exact reviewed head through GitHub's pull-request merge path.
6. Verify `main` advanced to the expected merge result.

Production deployment remains a separate authority: only the canonical exact-SHA deployment path may deploy a green commit already merged to `main`.
Repository protection does not create a second release or deployment authority.

## Negative proof

On 2026-09-03, after enabling protection, an empty probe commit based on `main@5d58d9b780f8a74c3993c8671707b0b4638eb99d` attempted a direct push to `main`.
GitHub rejected it with `GH006`, stating that changes must be made through a pull request and that 11 of 11 required status checks were expected.
Remote `main` remained unchanged.

A live force-push or branch-deletion probe is deliberately not executed because a misconfiguration would make the probe destructive.
Their denial is verified from the branch-protection API (`allow_force_pushes=false`, `allow_deletions=false`).

## Break-glass

There is no persistent bypass.
If a GitHub platform incident makes the protected PR path impossible, the repository owner may temporarily alter protection only as an explicit emergency operation.
The reason, exact prior protection state, temporary change, and restoration must be recorded; protection must be restored before normal production-code integration resumes.
A weakened-protection interval is never itself authority to deploy production.

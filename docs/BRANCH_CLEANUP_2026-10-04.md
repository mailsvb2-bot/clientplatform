# Audited stale branch cleanup — 2026-10-04

Purpose: restore the repository's canonical single-`main` topology without deleting any ref on inference alone.

The normal `single-main-topology` workflow deletes only a non-`main` branch whose current head SHA exactly matches the head SHA of a merged PR. The branches below are historical exceptions that were reviewed individually. The cleanup extension is intentionally fail-closed: both branch name and the exact audited SHA must match, the ref is re-read immediately before deletion, and any open PR preserves the branch.

| Branch | Audited SHA | Evidence / disposition |
|---|---|---|
| `diag/live-yandex-http400` | `b8aa1bc7bb470073d74673e0fde60de25ee40032` | Old Yandex HTTP-400 diagnostic line; later Yandex provider/auth hardening superseded this diagnostic path. |
| `feature/visual-provider-prompt-adapters` | `f971fde0ee3ad45f6ab4e4cf21d091099800d7b9` | Closed PR #505 explicitly superseded by canonical current-main rebase #508. |
| `feature/visual-semantic-auto-retry` | `544392ab37fe81edb829f9bcabd57e70aac87bc2` | Head is already an ancestor of current `main` (ahead-by 0); no unique tree changes remain. |
| `feature/visual-semantic-quality-gate` | `64d896336e689c5753cc52c093e28c77dd769dd7` | Old experimental semantic-quality line; canonical semantic QA/retry contracts landed later (#555 and subsequent scene/QA work). |
| `feature/visual-style-intent-v1` | `297a8e38aa26a8ba5f31792677dd4bd9bd048b1e` | Closed PR #503 explicitly superseded by #506. |
| `fix/canonical-owner-navigation-runtime` | `e0b3b446e88e8af5eada2db4eca70feba3c1a624` | Closed PR #448 explicitly superseded by merged owner-navigation work #450/#452. |
| `fix/operator-alert-chat-isolation` | `80f4a2f6c64eb5e6d64226f224786fcdb048b8cc` | Closed PR #531 explicitly superseded by #535, merged into main. |
| `fix/production-disk-worktree-audit-20261004` | `285ea2b8af0999cdc7c252e57e5c146fc7211a34` | Closed PR #569 replaced by reviewed and merged PR #571, which fixes the same area fail-closed. |
| `fix/single-owner-menu-root` | `c424505211fec441c931c582a8b80a8a83ec8a12` | Closed PR #449 explicitly superseded by #452. |
| `fix/visual-callback-safety-boundary` | `ba438820217bd41ed6e2bbd39c648a77826e66fa` | Closed PR #488 explicitly superseded by #487; old patch would drift callback contracts. |
| `fix/visual-provider-production-diagnostic` | `89d4ad2c711aec8430158f46ea3b38054fcde110` | Closed PR #484 explicitly superseded by #491–#494 and later diagnostic hardening. |
| `fix/yandex-visual-permission-diagnostic` | `ce2806604ef188849577fb37293e5562c8443ca4` | Closed PR #532 explicitly superseded by #533/#536; extra probe no longer closes a live defect. |
| `ops/deploy-40a5af2-production` | `7ffbf9e607751a6da9255d78bb7c79433c020d88` | Closed marker-only PR #521; no source-tree changes. Later fixes/deploy flow superseded it. |
| `ops/deploy-visual-delivery-hotfix` | `93701326ebebe13cc6510282b38522daca57e2a9` | Closed marker-only PR #504 explicitly superseded by #507; no source-tree changes. |
| `ops/production-visual-live-smoke` | `3ab6215df1a5d3ed4e25a24907545b7ead679eef` | Closed PR #470 was intentionally rejected because it could create a paid production AI job outside the canonical consent boundary. |
| `recover-production-deploy/image-framing-visual-diagnostic` | `66b142cdc75a68660f568713b26dc31d5e86585d` | Closed PR #468 as redundant; production repair already deployed and this carrier had no source-tree changes. |
| `revert/production-disk-audit-567` | `a0cd1e01f303f042260eea3b70f7896736b8b6d5` | Temporary revert branch for the earlier disk-audit attempt; #571 is now the canonical implementation. |

## Safety contract

Deletion is permitted only when one of these statements is true:

1. the branch's current SHA exactly equals the head SHA of a merged PR targeting `main`; or
2. the branch name and current SHA exactly equal one row in the audited stale-ref allowlist above.

In both cases the workflow:
- refuses deletion while an open PR exists;
- re-reads the Git ref immediately before deletion and refuses if the SHA moved;
- re-checks for an open PR immediately before deletion;
- verifies that only `main` remains;
- reports `ops/single-main-topology` failure if any non-`main` ref cannot be proven safe.

This audit changes GitHub refs only. It does not access or mutate any production server.

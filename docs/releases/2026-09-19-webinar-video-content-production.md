# Webinar video-content production carrier

This release carrier records deployment of main commit `05eed300fd6c141ebb7f8d7b79768341e36c9b53` after PR #373.

It contains no product logic changes. The merge commit is intentionally marked `[recover-production-deploy]` so the canonical production recovery workflow deploys the exact protected `main` SHA with webinar video content across announcement, warmup, and follow-up flows, including AI-prepared and owner-uploaded video with revision-safe provider dispatch.

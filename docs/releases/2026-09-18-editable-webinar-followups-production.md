# Editable webinar follow-ups production carrier

This release carrier records deployment of main commit `0444042a6d2ef934ffefffb2b75b86fb0ab6bbfd` after PR #370.

It contains no product logic changes. The merge commit is intentionally marked `[recover-production-deploy]` so the canonical production recovery workflow deploys the exact protected `main` SHA with owner-editable post-event follow-ups, Telegram/VK/MAX content-plan parity, revision-safe follow-up dispatch, and the webinar wizard callback-state fix.

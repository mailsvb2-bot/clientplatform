# Webinar content-plan production carrier

This release carrier records deployment of main commit `0204f843bb302fc2af8295ac0a613bb72a3e8b75` after PR #368.

It contains no product logic changes. The merge commit is intentionally marked `[recover-production-deploy]` so the canonical production recovery workflow deploys the exact protected `main` SHA with editable warmups, shared content-plan UI, Telegram/VK/MAX parity, and consent-aware autosend safety.

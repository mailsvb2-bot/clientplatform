# Production deploy carrier — MAX recovery

This documentation-only carrier deploys the current exact-green ClientPlatform main after PR #410.

Target capability: recover and enable the production MAX owner entry using provider-verified historical credentials without exposing secrets.

The production recovery workflow remains responsible for exact-source synchronization and fail-closed rollout.

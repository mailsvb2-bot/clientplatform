# Post-release topology cleanup

Code-neutral carrier to restore the repository to the canonical single `main` branch after the webinar video production release.

The merge commit is intentionally marked `[cleanup-branches]`; the repository topology workflow deletes all non-main branches and verifies that only `main` remains.

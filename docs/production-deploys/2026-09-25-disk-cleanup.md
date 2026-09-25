# ClientPlatform production disk cleanup — 2026-09-25

Application source stays `f3002198342f2642b5fe5111c9af1656f8710742`. This commit does not deploy.

The rollout of that SHA stopped after recreate and rolled back:

`CLIENTPLATFORM_PRODUCTION_DEPLOY_FAILED:insufficient_disk_capacity_after_deploy_cleanup`

with disk warnings 72.31% and 73.88%. Rollback completed (`CLIENTPLATFORM_PRODUCTION_ROLLBACK_OK`).

This cleanup is the repository's own maintenance script: Docker build cache kept to 512MB, journal vacuum to 256MB, and apt cache clean. It does not prune images, containers, networks, volumes, backups, or application data.

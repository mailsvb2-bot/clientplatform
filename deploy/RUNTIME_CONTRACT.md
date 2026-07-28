# Metrotherapy production runtime contract

This deployment keeps Telegram on polling. Do not switch Telegram to webhook for this project unless the production contract is deliberately changed and tested.

## Required production mode

- `APP_ENV=prod`
- `TELEGRAM_TRANSPORT=polling`
- `TELEGRAM_WEBHOOK_ENABLED=0`
- `HEALTHCHECK_ENABLED=1`
- `HEALTHCHECK_HOST=127.0.0.1`
- `HEALTHCHECK_PORT=8082`
- `PRIVACY_EXPORT_HTTP_ENABLED=1`
- `PRIVACY_EXPORT_PUBLIC_BASE_URL=https://<public-host>`
- `PRIVACY_EXPORT_TOKEN_TTL_MINUTES=10` (accepted range: 2..30)

## Optional local ingress runtime

The aiohttp ingress runtime may still be enabled for non-Telegram surfaces:

- MAX webhook
- VK webhook
- YooKassa web/reconciliation endpoints
- public payment terms (`/terms`)
- audio media/access links
- one-time privacy export confirmation/download links

Use:

- `MESSENGER_WEBHOOK_ENABLED=1`
- `MESSENGER_WEBHOOK_HOST=127.0.0.1`
- `MESSENGER_WEBHOOK_PORT=8081`
- `MESSENGER_PUBLIC_BASE_URL=https://<public-host>`

This does not imply Telegram webhook mode. Telegram remains polling.

The live `/etc/metrotherapy/metrotherapy.env` file is authoritative and is not
replaced by immutable deploys. Prepare the first rollout without manually editing
secrets:

```bash
cd /root/metrotherapy
git fetch --prune origin
git checkout main
git merge --ff-only origin/main
sudo bash scripts/prepare_privacy_export_rollout.sh
```

The helper takes an exclusive lock, preserves all unrelated bytes, writes a
timestamped backup, atomically updates only the three privacy-export keys, and
runs `runtime_contract.py` without restarting the service. Later immutable
deploys repeat this idempotent migration automatically after the fast-forward and
before candidate build or runtime switching.

## Runtime state must live outside the repository

Production must not write state into the project tree. Required:

- `METRO_DB_PATH=/var/lib/metrotherapy/data.db` for SQLite mode, or use Postgres with external state
- `LOG_PATH=/var/log/metrotherapy/app.log`

Do not use:

- `data/data.db`
- `logs/app.log`
- any `.env` file committed or shipped inside the repo

## Preflight checks

Before deploy or ad traffic:

```bash
python scripts/runtime_contract.py
python scripts/prod_readiness_check.py
python scripts/validate_project.py
python scripts/smoke.py
python -m pytest -q
```

`runtime_contract.py` is the explicit guard for this policy: polling-only Telegram, no Telegram webhook flag, non-colliding health/messenger ports, and out-of-tree runtime state in prod.


## ClientPlatform canonical interface

The Telegram control bot and dispatch runtime are enabled by default:

```text
CLIENTPLATFORM_CONTROL_BOT_ENABLED=1
CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED=1
```

Set both values to `0` only for an explicit emergency rollback. Missing values do
not return the application to the imported legacy interface. Runtime readiness
requires the complete ClientPlatform schema, including `booking_slots`.

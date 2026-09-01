# ClientPlatform: production isolation and polling gateway runbook

## Production boundary

ClientPlatform production is separate from any imported baseline deployment. It requires its own control Telegram bot, PostgreSQL database and roles, private S3-compatible bucket, HTTPS domain, Linux service account, state directories, backups, and staging/production secrets.

Fixed paths:

- immutable releases: `/var/lib/clientplatform/runtime/releases/<sha>`;
- writable state: `/var/lib/clientplatform/state`;
- logs: `/var/log/clientplatform`;
- app environment: `/etc/clientplatform/clientplatform.env` with mode `0600`;
- PostgreSQL backups: `/var/backups/clientplatform/postgres`.

The application role must be `NOSUPERUSER NOCREATEDB NOCREATEROLE`. Restore drills use a different operator-only administrative DSN. Managed Client Bots share the same application process and database, but every active bot route is globally unique, business-scoped, secret-reference-only and admitted through the durable Bot Gateway.

Telegram is polling-only for both the central control bot and all managed Telegram bots. VK and MAX use the separate messenger webhook server.

## External provisioning

Provision outside the repository:

1. A production control Telegram bot not used by staging or another product.
2. A dedicated PostgreSQL database plus least-privilege application role.
3. A separate restore-administrator role used only during drills.
4. A private S3-compatible production bucket with versioning, lifecycle retention, and replication to a separate failure domain.
5. A dedicated DNS name and TLS termination for VK/MAX, media, privacy and payment HTTP surfaces.
6. Independent diagnostics, media-signing and S3 secrets.
7. For every personal Telegram bot: an existing bot account and token stored under the `CLIENTPLATFORM_SECRET_TELEGRAM_*` namespace. Database records contain only `secret://env/...` references.
8. For every enabled VK/MAX provider: its own webhook verification secret and sender credentials.

Telegram's ordinary Bot API does not create bot accounts. BotFather fallback must place the token directly into the secret store; it must never send raw token material through ClientPlatform forms, callbacks, logs or database payloads.

Copy and fill the application environment:

```bash
sudo install -d -o root -g clientplatform -m 0750 /etc/clientplatform
sudo install -o root -g clientplatform -m 0600 \
  deploy/clientplatform/clientplatform.production.env.example \
  /etc/clientplatform/clientplatform.env
python scripts/clientplatform_production_preflight.py \
  --env-file /etc/clientplatform/clientplatform.env
python scripts/clientplatform_bot_gateway_preflight.py \
  --env-file /etc/clientplatform/clientplatform.env
```

Deployment is blocked until both commands print their corresponding `*_PREFLIGHT_OK` marker.

## Canonical transport settings

The production environment must contain:

```text
TELEGRAM_TRANSPORT=polling
RUN_MODE=polling
TELEGRAM_WEBHOOK_ENABLED=0
TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED=0
CLIENTPLATFORM_BOT_GATEWAY_ENABLED=1
```

`TELEGRAM_WEBHOOK_PUBLIC_BASE_URL`, `TELEGRAM_WEBHOOK_SECRET_TOKEN` and `TELEGRAM_WEBHOOK_PREFIX` must be empty or absent. Production preflight rejects them.

`MESSENGER_WEBHOOK_ENABLED=1` remains independent. Enable `VK_WEBHOOK_ENABLED` and `MAX_WEBHOOK_ENABLED` only for configured providers. Caddy publishes `/webhooks/*`; it does not publish a Telegram route.

## Systemd deployment

```bash
sudo useradd --system --home /var/lib/clientplatform --shell /usr/sbin/nologin clientplatform || true
sudo install -d -o root -g clientplatform -m 0750 \
  /var/lib/clientplatform/runtime/releases
sudo install -d -o clientplatform -g clientplatform -m 0750 \
  /var/lib/clientplatform/state \
  /var/lib/clientplatform/restore-evidence \
  /var/log/clientplatform
sudo install -d -o clientplatform -g clientplatform -m 0700 \
  /var/backups/clientplatform/postgres
```

Build an immutable release:

```bash
RELEASE=/var/lib/clientplatform/runtime/releases/$(git rev-parse HEAD)
sudo install -d -o root -g clientplatform -m 0750 "$RELEASE"
git archive HEAD | sudo tar -x -C "$RELEASE"
sudo python3.12 -m venv "$RELEASE/.venv"
sudo "$RELEASE/.venv/bin/pip" install --require-hashes -r "$RELEASE/requirements.txt"
sudo chown -R root:clientplatform "$RELEASE"
sudo chmod -R u=rwX,g=rX,o= "$RELEASE"
sudo -u clientplatform "$RELEASE/.venv/bin/python" \
  "$RELEASE/scripts/clientplatform_production_preflight.py" \
  --env-file /etc/clientplatform/clientplatform.env
sudo -u clientplatform "$RELEASE/.venv/bin/python" \
  "$RELEASE/scripts/clientplatform_bot_gateway_preflight.py" \
  --env-file /etc/clientplatform/clientplatform.env
```

Install units and reverse proxy configuration:

```bash
sudo install -m 0644 deploy/clientplatform/clientplatform.service /etc/systemd/system/
sudo install -m 0644 deploy/clientplatform/clientplatform-backup.service /etc/systemd/system/
sudo install -m 0644 deploy/clientplatform/clientplatform-backup.timer /etc/systemd/system/
sudo install -m 0644 deploy/clientplatform/Caddyfile /etc/caddy/Caddyfile.d/clientplatform.caddy
sudo systemctl daemon-reload
```

The service starts one central Telegram polling owner and one managed-bot polling task per active managed route. Caddy only forwards webhook-native providers and other reviewed HTTP surfaces.

## Migration and atomic switch

Load only the protected application environment, create a backup, then initialize the candidate release:

```bash
set -a
. /etc/clientplatform/clientplatform.env
set +a
sudo -u clientplatform --preserve-env=DATABASE_URL,CLIENTPLATFORM_BACKUP_DIR,CLIENTPLATFORM_BACKUP_RETENTION_DAYS \
  /var/lib/clientplatform/runtime/current/.venv/bin/python \
  /var/lib/clientplatform/runtime/current/scripts/clientplatform_postgres_backup.py backup
cd "$RELEASE"
sudo -u clientplatform --preserve-env=DATABASE_URL,CLIENTPLATFORM_DB_ENGINE \
  .venv/bin/python -c 'from services.schema import init_db; init_db()'
sudo -u clientplatform .venv/bin/python scripts/clientplatform_production_preflight.py \
  --env-file /etc/clientplatform/clientplatform.env
sudo -u clientplatform .venv/bin/python scripts/clientplatform_bot_gateway_preflight.py \
  --env-file /etc/clientplatform/clientplatform.env
sudo ln -sfn "$RELEASE" /var/lib/clientplatform/runtime/current.next
sudo mv -Tf /var/lib/clientplatform/runtime/current.next /var/lib/clientplatform/runtime/current
sudo systemctl restart clientplatform
```

Schema changes follow expand/contract. Destructive contract migrations require a separate reviewed migration, explicit export, and verified restore evidence. Never roll application code backward across a destructive migration.

## Registering a personal Telegram bot

Before activation, prove all of the following:

- the Telegram bot ID is not already registered anywhere in production;
- the business has no other active managed Telegram bot;
- the token reference resolves from the production secret provider;
- the connection and managed-bot rows belong to the same business and platform;
- `getMe` returns the expected immutable bot ID and username;
- `deleteWebhook(drop_pending_updates=False)` succeeds;
- gateway health reports `transport=polling` and an active poller;
- a `/start` update opens only that business's customer portal;
- a test program's first and next materials are both sent through that managed connection.

Disable or revoke the old route before replacing a business bot. Never temporarily allow two active Telegram bots for one business.

## Post-deploy evidence

Check the central process and health endpoints:

```bash
systemctl is-active clientplatform
journalctl -u clientplatform --since '-10 minutes' --no-pager
python scripts/clientplatform_http_probe.py synthetic \
  --health-base-url http://127.0.0.1:8182 \
  --public-base-url "https://$CLIENTPLATFORM_DOMAIN"
python scripts/clientplatform_http_probe.py load-smoke \
  --health-base-url http://127.0.0.1:8182 \
  --requests 200 --concurrency 8 --max-p95-ms 500
```

Then perform provider-specific probes:

1. Call Telegram `getWebhookInfo` for the central bot and every managed bot; `url` must be empty.
2. Send `/start` to the central bot and prove ClientPlatform responds.
3. Send `/start` to every managed test bot and prove only its linked business portal opens.
4. Inspect gateway counters: `active_pollers`, `pending`, `processing`, `retry`, `processed`, `dead`, `polling_conflicts`.
5. Replay sanitized VK/MAX webhook fixtures twice and prove the second event does not repeat tenant or outbox side effects.
6. Prove a failure in one managed polling task does not stop another bot.

An HTTP `200` from VK/MAX alone is insufficient; inspect dedupe and delivery-outbox state.

## Backup and disposable restore drill

The timer creates a PostgreSQL custom-format dump, SHA-256 checksum, and metadata with mode `0600`. The backup filesystem or remote target must provide encryption at rest.

The application environment deliberately does not contain the restore administrator. Inject `CLIENTPLATFORM_RESTORE_ADMIN_DATABASE_URL` only for the drill:

```bash
sudo systemctl enable --now clientplatform-backup.timer
sudo systemctl start clientplatform-backup.service
LATEST=$(find /var/backups/clientplatform/postgres -name 'clientplatform-*.dump' -printf '%T@ %p\n' \
  | sort -nr | head -1 | cut -d' ' -f2-)
pg_restore --list "$LATEST" | grep -E 'TABLE DATA public (managed_bots|bot_gateway_ingress_events)'
read -r -s -p 'Restore administrator DSN: ' CLIENTPLATFORM_RESTORE_ADMIN_DATABASE_URL
export CLIENTPLATFORM_RESTORE_ADMIN_DATABASE_URL
set -a
. /etc/clientplatform/clientplatform.env
set +a
sudo -u clientplatform --preserve-env=DATABASE_URL,CLIENTPLATFORM_RESTORE_ADMIN_DATABASE_URL,CLIENTPLATFORM_RESTORE_EVIDENCE_DIR \
  /var/lib/clientplatform/runtime/current/.venv/bin/python \
  /var/lib/clientplatform/runtime/current/scripts/clientplatform_postgres_backup.py \
  restore-drill "$LATEST"
unset CLIENTPLATFORM_RESTORE_ADMIN_DATABASE_URL
```

The drill verifies the checksum, creates a disposable database, restores the dump, checks canonical ClientPlatform tables, writes sanitized evidence, and drops the disposable database. The archive-list check proves that managed-bot registry and durable ingress tables are present in the tested dump.

## Rollback

1. To isolate one failing personal bot, disable only its managed-bot route; the reconciler stops its polling task.
2. If a duplicate polling owner exists, stop the duplicate process before restarting the canonical service.
3. For application rollback, stop `clientplatform.service`.
4. For additive migrations, atomically repoint `current` to the previous release.
5. For data restoration, preserve the failed database and restore the verified dump into a new database. Never overwrite the only failed-state copy.
6. Run both preflights, health/readiness, Telegram `/start`, VK/MAX entry, fleet isolation and load smoke again.
7. Record release SHA, bot/business route, backup checksum, restore evidence, incident cause, and decision owner.

Do not enable Telegram webhook as a rollback measure.

## Docker Compose alternative

`deploy/clientplatform/Dockerfile.dockerignore` excludes `clientplatform.env`, generic `.env*`, databases, backups, and private-key formats from the image context.

```bash
cd deploy/clientplatform
cp clientplatform.production.env.example clientplatform.env
chmod 0600 clientplatform.env
export CLIENTPLATFORM_POSTGRES_ADMIN_PASSWORD='from-admin-secret-store'
export CLIENTPLATFORM_POSTGRES_APP_PASSWORD='from-app-secret-store'
export CLIENTPLATFORM_DOMAIN='clientplatform.your-domain.ru'
docker compose --env-file clientplatform.env -f compose.production.yml config
docker compose --env-file clientplatform.env -f compose.production.yml up -d --build
```

`--env-file clientplatform.env` is mandatory for manual Compose operations so interpolation uses the same production route and feature settings as the canonical updater; operator-provided secret-store exports remain explicit overrides.

The PostgreSQL container creates `clientplatform_app` as `NOSUPERUSER NOCREATEDB NOCREATEROLE`. Only Caddy publishes ports. The application container runs both production preflights before `main.py` and may write only to mounted ClientPlatform state/log/backup volumes.

## Go-live gate

Production traffic remains blocked until:

- both offline preflights pass;
- central and managed Telegram webhook URLs are empty;
- exactly one polling owner consumes each Telegram token;
- `/start` works in the central bot and managed test bot;
- `/start` and provider-native start events work through enabled VK/MAX webhook adapters;
- the dedicated application role has no superuser, `CREATEDB`, or `CREATEROLE` privileges;
- schema initialization passes as the application role;
- backup and disposable restore drill pass with separate roles and the dump contains gateway tables;
- health, readiness and fleet counters are green;
- identical managed-bot admission and VK/MAX webhook replay are idempotent;
- PostgreSQL two-connection admission and claim matrices pass;
- bounded load smoke meets the recorded threshold;
- staging uses different control bot, managed-bot tokens, database, bucket, domain, and secrets;
- a failure in one bot is proven not to stop another bot;
- rollback commands and owner are recorded;
- external automatic bot provisioning remains disabled until its provider adapter and token-to-secret-store transfer are separately reviewed.

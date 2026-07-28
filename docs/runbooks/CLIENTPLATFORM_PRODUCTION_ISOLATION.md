# ClientPlatform: production isolation runbook

## Production boundary

ClientPlatform production is separate from the imported Metrotherapy deployment. It requires its own Telegram bot, PostgreSQL database and roles, private S3-compatible bucket, HTTPS domain, webhook secret, Linux service account, state directories, backups, and staging/production secrets.

Fixed paths:

- immutable releases: `/var/lib/clientplatform/runtime/releases/<sha>`;
- writable state: `/var/lib/clientplatform/state`;
- logs: `/var/log/clientplatform`;
- app environment: `/etc/clientplatform/clientplatform.env` with mode `0600`;
- PostgreSQL backups: `/var/backups/clientplatform/postgres`.

The application role must be `NOSUPERUSER NOCREATEDB NOCREATEROLE`. Restore drills use a different operator-only administrative DSN. Managed Client Bots are not enabled in this PR; they require the next Bot Gateway boundary.

## External provisioning

Provision outside the repository:

1. A production Telegram bot not used by staging or another product.
2. A dedicated PostgreSQL database plus least-privilege application role.
3. A separate restore-administrator role used only during drills.
4. A private S3-compatible production bucket with versioning, lifecycle retention, and replication to a separate failure domain.
5. A dedicated DNS name and TLS termination.
6. Independent webhook, diagnostics, media-signing, and S3 secrets.

Copy and fill the application environment:

```bash
sudo install -d -o root -g clientplatform -m 0750 /etc/clientplatform
sudo install -o root -g clientplatform -m 0600 \
  deploy/clientplatform/clientplatform.production.env.example \
  /etc/clientplatform/clientplatform.env
python scripts/clientplatform_production_preflight.py \
  --env-file /etc/clientplatform/clientplatform.env
```

Deployment is blocked until the last command prints `CLIENTPLATFORM_PRODUCTION_PREFLIGHT_OK`.

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
```

Install the units and reverse proxy configuration:

```bash
sudo install -m 0644 deploy/clientplatform/clientplatform.service /etc/systemd/system/
sudo install -m 0644 deploy/clientplatform/clientplatform-backup.service /etc/systemd/system/
sudo install -m 0644 deploy/clientplatform/clientplatform-backup.timer /etc/systemd/system/
sudo install -m 0644 deploy/clientplatform/Caddyfile /etc/caddy/Caddyfile.d/clientplatform.caddy
sudo systemctl daemon-reload
```

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
sudo -u clientplatform --preserve-env=DATABASE_URL,METRO_DB_ENGINE \
  .venv/bin/python -c 'from services.schema import init_db; init_db()'
sudo -u clientplatform .venv/bin/python scripts/clientplatform_production_preflight.py \
  --env-file /etc/clientplatform/clientplatform.env
sudo ln -sfn "$RELEASE" /var/lib/clientplatform/runtime/current.next
sudo mv -Tf /var/lib/clientplatform/runtime/current.next /var/lib/clientplatform/runtime/current
sudo systemctl restart clientplatform
```

Schema changes follow expand/contract. Destructive contract migrations require a separate reviewed migration, explicit export, and verified restore evidence. Never roll application code backward across a destructive migration.

## Post-deploy evidence

```bash
python scripts/clientplatform_http_probe.py synthetic \
  --health-base-url http://127.0.0.1:8182 \
  --public-base-url "https://$CLIENTPLATFORM_DOMAIN"
python scripts/clientplatform_http_probe.py load-smoke \
  --health-base-url http://127.0.0.1:8182 \
  --requests 200 --concurrency 8 --max-p95-ms 500
python scripts/clientplatform_http_probe.py replay /secure/evidence/webhook-replay.jsonl \
  --public-base-url "https://$CLIENTPLATFORM_DOMAIN" \
  --webhook-secret "$TELEGRAM_WEBHOOK_SECRET_TOKEN" \
  --repetitions 2
```

Replay fixtures contain only sanitized Telegram updates with synthetic IDs. After replay, inspect database/outbox counters and prove the absence of duplicate domain effects; HTTP `200` alone is insufficient.

## Backup and disposable restore drill

The timer creates a PostgreSQL custom-format dump, SHA-256 checksum, and metadata with mode `0600`. The backup filesystem or remote target must provide encryption at rest.

The application environment deliberately does not contain the restore administrator. Inject `CLIENTPLATFORM_RESTORE_ADMIN_DATABASE_URL` only for the drill:

```bash
sudo systemctl enable --now clientplatform-backup.timer
sudo systemctl start clientplatform-backup.service
LATEST=$(find /var/backups/clientplatform/postgres -name 'clientplatform-*.dump' -printf '%T@ %p\n' \
  | sort -nr | head -1 | cut -d' ' -f2-)
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

The drill verifies the checksum, creates a disposable database, restores the dump, checks canonical ClientPlatform tables, writes sanitized evidence, and drops the disposable database.

## Rollback

1. Stop public traffic or disable the Telegram webhook.
2. Stop `clientplatform.service`.
3. For additive migrations, atomically repoint `current` to the previous release.
4. For data restoration, preserve the failed database and restore the verified dump into a new database. Never overwrite the only failed-state copy.
5. Run preflight, health/readiness, synthetic, replay, and load smoke again.
6. Record release SHA, backup checksum, restore evidence, incident cause, and decision owner.

## Docker Compose alternative

`.dockerignore` excludes `clientplatform.env`, generic `.env*`, databases, backups, and private-key formats from the image context.

```bash
cd deploy/clientplatform
cp clientplatform.production.env.example clientplatform.env
chmod 0600 clientplatform.env
export CLIENTPLATFORM_POSTGRES_ADMIN_PASSWORD='from-admin-secret-store'
export CLIENTPLATFORM_POSTGRES_APP_PASSWORD='from-app-secret-store'
export CLIENTPLATFORM_DOMAIN='clientplatform.your-domain.ru'
docker compose -f compose.production.yml config
docker compose -f compose.production.yml up -d --build
```

The PostgreSQL container creates `clientplatform_app` as `NOSUPERUSER NOCREATEDB NOCREATEROLE`. Only Caddy publishes ports. The application container runs the production preflight before `main.py` and may write only to mounted ClientPlatform state/log/backup volumes.

## Go-live gate

Production traffic remains blocked until:

- offline preflight passes;
- the dedicated application role has no superuser, `CREATEDB`, or `CREATEROLE` privileges;
- schema initialization passes as the application role;
- backup and disposable restore drill pass with separate roles;
- health and readiness are green;
- invalid webhook secrets are rejected;
- sanitized replay has no duplicate domain effects;
- bounded load smoke meets the recorded threshold;
- staging uses different bot, database, bucket, domain, and secrets;
- rollback commands and owner are recorded;
- Managed Client Bots remain disabled until Bot Gateway adds bot routing, replay protection, tenant rate limits, queueing, fleet health, and failure isolation.

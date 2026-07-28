# ClientPlatform: production isolation runbook

## Purpose

This runbook creates a production boundary that is separate from the imported Metrotherapy deployment. ClientPlatform must have its own Telegram bot, PostgreSQL database and role, private S3-compatible bucket, domain, webhook secret, service account, runtime directories, backups and staging/production secrets.

The repository never creates or stores provider secrets. Operators provision them in the production secret store or `/etc/clientplatform/clientplatform.env` with mode `0600`.

## Fixed boundaries

- Linux user/group: `clientplatform`.
- Immutable runtime releases: `/var/lib/clientplatform/runtime/releases/<sha>`.
- Writable state: `/var/lib/clientplatform/state`.
- Configuration: `/etc/clientplatform/clientplatform.env`.
- Logs: `/var/log/clientplatform`.
- PostgreSQL database: a dedicated name beginning with `clientplatform`.
- PostgreSQL application role: no `CREATEDB`, no superuser. A separate operator-only role is used for restore drills.
- S3 bucket: one production bucket beginning with `clientplatform-`; staging uses another bucket and credentials.
- Public domain: a dedicated HTTPS host.
- Public routes: Telegram webhook and signed media only. Health/readiness stay loopback-only.
- Current production mode: one dedicated ClientPlatform bot through a protected webhook. Managed Client Bots require the next Bot Gateway PR and must not be emulated by adding polling processes.

## Provision external resources

1. Create a production Telegram bot that is not used by staging or another product.
2. Create a PostgreSQL database and least-privilege application role. The role must own or have only the privileges required inside the ClientPlatform database. Create a separate administrative role for disposable restore drills.
3. Create a private S3-compatible bucket, enable versioning, lifecycle retention and backup replication to a separate failure domain.
4. Configure DNS for the dedicated domain.
5. Generate independent secrets for Telegram webhook verification, health diagnostics, media URL signing and S3 access.
6. Copy `deploy/clientplatform/clientplatform.production.env.example` to the protected application environment and replace every blank/placeholder.

Run the offline contract before any process starts:

```bash
python scripts/clientplatform_production_preflight.py \
  --env-file /etc/clientplatform/clientplatform.env
```

A production rollout is blocked until this prints `CLIENTPLATFORM_PRODUCTION_PREFLIGHT_OK`.

## Systemd deployment

```bash
sudo useradd --system --home /var/lib/clientplatform --shell /usr/sbin/nologin clientplatform || true
sudo install -d -o root -g clientplatform -m 0750 \
  /var/lib/clientplatform/runtime \
  /var/lib/clientplatform/runtime/releases
sudo install -d -o clientplatform -g clientplatform -m 0750 \
  /var/lib/clientplatform/state \
  /var/lib/clientplatform/restore-evidence \
  /var/log/clientplatform
sudo install -d -o clientplatform -g clientplatform -m 0700 \
  /var/backups/clientplatform/postgres
sudo install -d -o root -g clientplatform -m 0750 /etc/clientplatform
sudo install -o root -g clientplatform -m 0600 \
  deploy/clientplatform/clientplatform.production.env.example \
  /etc/clientplatform/clientplatform.env
```

Build each release in a content-addressed directory. Runtime-created files must remain under `/var/lib/clientplatform/state`, never inside `current` or under `/var/lib/metrotherapy`.

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

Install units and reverse proxy configuration:

```bash
sudo install -m 0644 deploy/clientplatform/clientplatform.service /etc/systemd/system/
sudo install -m 0644 deploy/clientplatform/clientplatform-backup.service /etc/systemd/system/
sudo install -m 0644 deploy/clientplatform/clientplatform-backup.timer /etc/systemd/system/
sudo install -m 0644 deploy/clientplatform/Caddyfile /etc/caddy/Caddyfile.d/clientplatform.caddy
sudo systemctl daemon-reload
```

## Migration and atomic release switch

Before every migration:

```bash
set -a
. /etc/clientplatform/clientplatform.env
set +a
sudo -u clientplatform --preserve-env=DATABASE_URL,CLIENTPLATFORM_BACKUP_DIR,CLIENTPLATFORM_BACKUP_RETENTION_DAYS \
  /var/lib/clientplatform/runtime/current/.venv/bin/python \
  /var/lib/clientplatform/runtime/current/scripts/clientplatform_postgres_backup.py backup
```

Initialize/upgrade the candidate against PostgreSQL before switching the symlink:

```bash
cd "$RELEASE"
sudo -u clientplatform --preserve-env=DATABASE_URL,METRO_DB_ENGINE \
  .venv/bin/python -c 'from services.schema import init_db; init_db()'
sudo -u clientplatform .venv/bin/python scripts/clientplatform_production_preflight.py \
  --env-file /etc/clientplatform/clientplatform.env
sudo ln -sfn "$RELEASE" /var/lib/clientplatform/runtime/current.next
sudo mv -Tf /var/lib/clientplatform/runtime/current.next /var/lib/clientplatform/runtime/current
sudo systemctl restart clientplatform
```

The schema policy is expand/contract: a release may add compatible structures, but destructive contract migrations require a separate reviewed migration with explicit data export and restore evidence. Never roll application code backward across a destructive migration.

## Post-deploy proof

```bash
python scripts/clientplatform_http_probe.py synthetic \
  --health-base-url http://127.0.0.1:8182 \
  --public-base-url "https://$CLIENTPLATFORM_DOMAIN"

python scripts/clientplatform_http_probe.py load-smoke \
  --health-base-url http://127.0.0.1:8182 \
  --requests 200 --concurrency 8 --max-p95-ms 500
```

A replay fixture must contain sanitized Telegram updates with synthetic IDs and no real message text, usernames, phones or tokens:

```bash
python scripts/clientplatform_http_probe.py replay /secure/evidence/webhook-replay.jsonl \
  --public-base-url "https://$CLIENTPLATFORM_DOMAIN" \
  --webhook-secret "$TELEGRAM_WEBHOOK_SECRET_TOKEN" \
  --repetitions 2
```

After the HTTP replay, verify database/outbox counters and absence of duplicate domain effects. HTTP `200` alone proves transport replay tolerance, not business idempotency.

## Backup and restore proof

The daily timer creates a PostgreSQL custom-format dump, SHA-256 checksum and metadata with mode `0600`. The backup filesystem or remote target must provide encryption at rest; plaintext credentials and dumps must never be copied to repository artifacts.

The application environment deliberately does not contain restore-administrator credentials. Inject `CLIENTPLATFORM_RESTORE_ADMIN_DATABASE_URL` only into the operator shell or a short-lived root-only environment during a drill. It must identify a separate role capable of creating and dropping the disposable restore database.

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

A successful drill restores into a disposable database, checks canonical ClientPlatform tables, writes sanitized evidence and drops the disposable database. Run it at least monthly and before a risky migration.

## Rollback

1. Stop incoming traffic at the reverse proxy or disable the Telegram webhook.
2. Stop `clientplatform.service`.
3. If the migration was additive, atomically repoint `current` to the previous release and start the service.
4. If data restoration is required, preserve the failed database, create a new database from the verified dump, run schema/readiness checks, then switch the application DSN. Do not overwrite the only copy of the failed state.
5. Run synthetic, replay and bounded load probes again.
6. Record release SHA, backup checksum, restore evidence path, incident cause and final decision.

## Docker Compose alternative

```bash
cd deploy/clientplatform
cp clientplatform.production.env.example clientplatform.env
chmod 0600 clientplatform.env
export CLIENTPLATFORM_POSTGRES_PASSWORD='from-secret-store'
export CLIENTPLATFORM_DOMAIN='clientplatform.your-domain.ru'
docker compose -f compose.production.yml config
docker compose -f compose.production.yml up -d --build
```

The Compose network allows container listeners on `0.0.0.0`, but only Caddy publishes ports. The app sets `CLIENTPLATFORM_DEPLOYMENT_MODE=container` and `CLIENTPLATFORM_CONTAINER_NETWORK_ISOLATED=1`; the preflight rejects wildcard binds outside that explicit container boundary. `.dockerignore` excludes the local `clientplatform.env`, generic `.env*` files, databases, backups and private-key formats from image context.

## Go-live gate

Production traffic remains blocked until all are true:

- offline production preflight passes;
- PostgreSQL schema initialization passes on the dedicated database;
- application role has no `CREATEDB` or superuser privilege;
- backup exists and a disposable restore drill passes using a separate operator-only DSN;
- health and readiness are green;
- invalid webhook secret is rejected;
- sanitized webhook replay has no duplicate domain effects;
- bounded load smoke meets the recorded threshold;
- staging uses different bot, database, bucket, domain and secrets;
- rollback owner and commands are recorded;
- Managed Client Bots are not enabled before Bot Gateway replay protection, bot routing, rate limits, queueing and failure isolation exist.

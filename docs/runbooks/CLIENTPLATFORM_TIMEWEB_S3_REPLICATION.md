# ClientPlatform: Timeweb S3 backup replication

This runbook continues the production setup after the primary Timeweb S3 bucket and its credentials have been created. It closes the gap between provider-internal redundancy and a separately addressable backup bucket.

## Storage boundary

Use two private, versioned buckets:

- primary: `clientplatform-production-8493913`;
- backup: `clientplatform-backup-8493913`.

The media gateway allowlist must continue to contain only the primary bucket. The backup bucket is never exposed through the media gateway.

Use a dedicated S3 user that can read and write only these two buckets. Do not paste its Access Key or Secret Key into chat, issues, commits, logs, or command history.

Timeweb connection values:

```text
Endpoint: https://s3.twcstorage.ru
Region: ru-1
```

Enable versioning on both buckets before running any proof or sync command. The replication job fails closed when either bucket does not report `Enabled`.

## Environment

Open `/etc/clientplatform/clientplatform.env` and set:

```text
CLIENTPLATFORM_STORAGE_BUCKET=clientplatform-production-8493913
CLIENTPLATFORM_MEDIA_GATEWAY_ALLOWED_BUCKETS=clientplatform-production-8493913
CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT=https://s3.twcstorage.ru
CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION=ru-1
CLIENTPLATFORM_S3_BACKUP_BUCKET=clientplatform-backup-8493913
CLIENTPLATFORM_S3_REPLICATION_EVIDENCE_DIR=/var/lib/clientplatform/s3-replication-evidence
CLIENTPLATFORM_S3_REPLICATION_TIMEOUT_SEC=30
CLIENTPLATFORM_S3_REPLICATION_MAX_COPY_BYTES=5000000000
CLIENTPLATFORM_S3_BACKUP_REPLICATION_ENABLED=0
```

Store the credentials only in the protected environment file:

```bash
read -r -s -p "S3 Access Key: " S3_ACCESS; echo
read -r -s -p "S3 Secret Key: " S3_SECRET; echo

grep -v '^CLIENTPLATFORM_SECRET_S3_ACCESS_KEY=' /etc/clientplatform/clientplatform.env \
  > /etc/clientplatform/clientplatform.env.tmp
printf 'CLIENTPLATFORM_SECRET_S3_ACCESS_KEY=%s\n' "$S3_ACCESS" \
  >> /etc/clientplatform/clientplatform.env.tmp
mv /etc/clientplatform/clientplatform.env.tmp /etc/clientplatform/clientplatform.env

grep -v '^CLIENTPLATFORM_SECRET_S3_SECRET_KEY=' /etc/clientplatform/clientplatform.env \
  > /etc/clientplatform/clientplatform.env.tmp
printf 'CLIENTPLATFORM_SECRET_S3_SECRET_KEY=%s\n' "$S3_SECRET" \
  >> /etc/clientplatform/clientplatform.env.tmp
mv /etc/clientplatform/clientplatform.env.tmp /etc/clientplatform/clientplatform.env

unset S3_ACCESS S3_SECRET
chmod 600 /etc/clientplatform/clientplatform.env
```

## Install the replication timer

```bash
sudo install -d -o clientplatform -g clientplatform -m 0700 \
  /var/lib/clientplatform/s3-replication-evidence
sudo install -m 0644 \
  deploy/clientplatform/clientplatform-s3-replication.service \
  /etc/systemd/system/
sudo install -m 0644 \
  deploy/clientplatform/clientplatform-s3-replication.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
```

## One-time live proof

Run the proof before changing the production evidence flag:

```bash
set -a
. /etc/clientplatform/clientplatform.env
set +a
sudo -u clientplatform --preserve-env \
  /var/lib/clientplatform/runtime/current/.venv/bin/python \
  /var/lib/clientplatform/runtime/current/scripts/clientplatform_s3_replication.py \
  prove
```

A successful run prints:

```text
CLIENTPLATFORM_S3_REPLICATION_PROOF_OK:/var/lib/clientplatform/s3-replication-evidence/latest.json
```

The proof performs all of the following:

1. reads versioning state from both buckets;
2. writes a random object under `.clientplatform-replication-probe/` in the primary bucket;
3. copies it to the backup bucket with AWS Signature Version 4;
4. downloads the backup copy and verifies its SHA-256;
5. verifies source ETag and size metadata on the backup object;
6. deletes the current probe objects from both buckets;
7. writes sanitized evidence with mode `0600`.

Because both buckets are versioned, deletion creates version history or delete markers. Configure lifecycle retention for old non-current probe versions if required.

Inspect the evidence without exposing credentials:

```bash
sudo -u clientplatform \
  /var/lib/clientplatform/runtime/current/.venv/bin/python \
  /var/lib/clientplatform/runtime/current/scripts/clientplatform_s3_replication.py \
  status
```

Only after the live proof succeeds, set:

```bash
sed -i \
  's/^CLIENTPLATFORM_S3_BACKUP_REPLICATION_ENABLED=.*/CLIENTPLATFORM_S3_BACKUP_REPLICATION_ENABLED=1/' \
  /etc/clientplatform/clientplatform.env
```

Then enable continuous replication:

```bash
sudo systemctl enable --now clientplatform-s3-replication.timer
sudo systemctl start clientplatform-s3-replication.service
systemctl status clientplatform-s3-replication.timer --no-pager
journalctl -u clientplatform-s3-replication.service -n 100 --no-pager
```

Run the production preflight again:

```bash
cd /var/lib/clientplatform/runtime/current
set -a
. /etc/clientplatform/clientplatform.env
set +a
python scripts/clientplatform_production_preflight.py --json
```

## Replication semantics

The scheduled job is intentionally backup-oriented:

- it copies missing or changed current objects from primary to backup;
- it verifies each copied object before reporting success;
- it never deletes objects merely because they disappeared from primary;
- it preserves old backup states through destination-bucket versioning;
- it writes no access keys, secret keys, object names, or payload data into evidence;
- it refuses non-HTTPS endpoints, shared bucket names, identical source and backup buckets, disabled versioning, oversized single-copy objects, and concurrent runs.

The timer runs every 15 minutes, so the intended recovery point objective is approximately 15 minutes. It is not provider-side continuous replication and it does not recreate every source-bucket version that existed and disappeared between scheduled runs.

## Recovery check

Periodically choose a real backup object in the Timeweb object manager and verify that it can be downloaded independently from the backup bucket. Keep the backup bucket private and never add it to `CLIENTPLATFORM_MEDIA_GATEWAY_ALLOWED_BUCKETS`.

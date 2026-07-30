# ClientPlatform production update

This runbook updates the dedicated Docker Compose deployment without exposing secrets.

## Guarantees

The updater:

1. locks concurrent deployments;
2. preserves untracked `clientplatform.env` and `.env` files;
3. requires a pinned expected Git commit when supplied;
4. validates and minimally augments the production environment;
5. creates an encrypted pre-deploy PostgreSQL backup when an age recipient exists;
6. permits a root-only local emergency dump only with the explicit `--allow-local-backup` flag;
7. tags the previous app image for rollback;
8. builds the new app and backup images;
9. recreates only `app` and `caddy`;
10. requires internal `/readyz`, production preflight markers, the dispatch runtime marker and an external HTTPS response;
11. restores the previous app image automatically if any post-recreate gate fails;
12. writes owner-only deployment evidence to `/var/lib/clientplatform/deploy-evidence/latest.json`.

## First update of a non-Git deployment

Use an immutable script URL and require the exact merged SHA:

```bash
CLIENTPLATFORM_EXPECTED_SHA=<MERGED_SHA> CLIENTPLATFORM_TARGET_REF=main CLIENTPLATFORM_ROOT=/opt/clientplatform bash -c 'curl -fsSL "https://raw.githubusercontent.com/mailsvb2-bot/clientplatform/$CLIENTPLATFORM_EXPECTED_SHA/deploy/clientplatform/update-production.sh" -o /root/clientplatform-update.sh && chmod 700 /root/clientplatform-update.sh && /root/clientplatform-update.sh --allow-local-backup'
```

`--allow-local-backup` is an explicit temporary exception for the first rollout when the age recovery recipient has not yet been configured. It creates a root-only local dump and checksum before changing the app image. It does not satisfy offsite disaster recovery.

## Normal update after age key custody is established

```bash
CLIENTPLATFORM_EXPECTED_SHA=<MERGED_SHA> sh /opt/clientplatform/deploy/clientplatform/update-production.sh
```

## Required age setup before enabling offsite backup

Generate an age X25519 identity on a trusted operator device, not in the application container. Store the private identity in at least two protected offline locations. Put only the public `age1...` recipient in:

```text
CLIENTPLATFORM_BACKUP_AGE_RECIPIENT=age1...
```

Then run the updater without `--allow-local-backup`. The pre-deploy backup must emit `CLIENTPLATFORM_ENCRYPTED_BACKUP_OK` before any application recreation.

## Success evidence

A successful run ends with:

```text
CLIENTPLATFORM_PRODUCTION_DEPLOY_OK:/var/lib/clientplatform/deploy-evidence/deploy-....json
```

Verify without printing secrets:

```bash
python3 -c 'import json; p="/var/lib/clientplatform/deploy-evidence/latest.json"; d=json.load(open(p)); print({k:d.get(k) for k in ("ok","target_sha","backup_mode","domain","completed_at")})'
```

## Rollback

Automatic rollback occurs when readiness, runtime markers or external TLS proof fails. The retained image tag is recorded in deployment evidence. Manual rollback should be exceptional and must use the recorded `rollback_tag`, followed by the same readiness checks.

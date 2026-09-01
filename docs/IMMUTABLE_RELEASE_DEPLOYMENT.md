# Immutable production releases

## Runtime layout

The production Git checkout remains the source and webhook-deploy control plane:

```text
/root/clientplatform
```

The running application is separate:

```text
/var/lib/clientplatform/runtime/
├── releases/
│   ├── <40-char-git-sha>/
│   │   ├── .venv/
│   │   ├── main.py
│   │   ├── requirements.txt or requirements-py313.txt
│   │   └── .release.json
│   └── ...
├── current  -> releases/<deployed-sha>
└── previous -> releases/<rollback-sha>
```

Licensed audio is application state, not release code. The default shared store is:

```text
/var/lib/clientplatform/audio
```

Each release contains an `audio` symlink to that shared store. Existing source-tree audio is copied into the shared store during the first immutable build.

## Sealing rules

A release is published only after all of the following are complete:

1. `git archive <sha>` creates a detached source snapshot.
2. A new `.venv` is created inside that release.
3. Runtime dependencies are installed from the Python-version-specific lock with `--require-hashes`.
4. Temporary venv paths are rewritten to the deterministic final release path.
5. Project bytecode is compiled with deterministic final `co_filename` paths.
6. The dependency lock SHA-256 and the complete release-tree SHA-256 are written to `.release.json`.
7. The staging directory is atomically renamed to `releases/<sha>`.
8. `scripts/immutable_release.py validate` recomputes both hashes before the release may be used.

The complete tree digest covers relative paths, file types, permission bits, symlink targets, and regular-file bytes. The marker itself is excluded to avoid a self-referential hash.

## Deployment sequence

`scripts/immutable_deploy.sh` performs this sequence:

1. Verify remote Git topology is exactly `1/main` without writing to GitHub.
2. Fast-forward the source checkout.
3. On the first rollout, build the previously deployed SHA and point `current` to it.
4. Install the systemd override that executes `/var/lib/clientplatform/runtime/current/.venv/bin/python`.
5. Build and seal the candidate SHA.
6. Run strict candidate validation. This applies expand-compatible migrations.
7. Run the previous release against the expanded schema and require schema readiness.
8. Atomically set `previous` to the old `current`, then set `current` to the candidate.
9. Restart the service and require local health, local readiness, and public health.
10. Run the mandatory production gate, including backup freshness, isolated restore drill, payment/concurrency probes, user journeys, health, and readiness.
11. Revalidate both sealed release trees.
12. Atomically write `deployment-proof.json`.
13. Only then update the canonical `deployed_sha` marker.

If any step after the switch fails, the error trap atomically switches `current` back to `previous` and restarts the service. It does not run `git reset`, reinstall dependencies, or mutate either release.

## Systemd contract

The service must execute the symlinked runtime, never the source checkout:

```ini
[Service]
WorkingDirectory=/var/lib/clientplatform/runtime/current
ExecStart=
ExecStart=/var/lib/clientplatform/runtime/current/.venv/bin/python /var/lib/clientplatform/runtime/current/main.py
Environment=PYTHONDONTWRITEBYTECODE=1
```

`scripts/immutable_deploy.sh` installs this as:

```text
/etc/systemd/system/clientplatform.service.d/immutable-release.conf
```

## Mandatory production environment

The real deployment still requires:

- `/etc/clientplatform/clientplatform.env`;
- PostgreSQL production storage;
- `CLIENTPLATFORM_RESTORE_DRILL_DATABASE_URL` or `RESTORE_DATABASE_URL` pointing to a different database whose name contains `drill`, `restore`, or `test`;
- a fresh disaster-recovery backup and checksum manifest;
- normal health/readiness endpoints.

The restore script refuses the production URL, the same database name, system databases, and targets without a drill/test marker.

### First privacy-export rollout

The authoritative env file is migrated with:

```bash
cd /root/clientplatform
git fetch --prune origin
git checkout main
git merge --ff-only origin/main
sudo bash scripts/prepare_privacy_export_rollout.sh
```

This command does not restart the service. It creates a timestamped backup,
atomically adds or repairs only `PRIVACY_EXPORT_HTTP_ENABLED`,
`PRIVACY_EXPORT_PUBLIC_BASE_URL`, and `PRIVACY_EXPORT_TOKEN_TTL_MINUTES`, then
runs the offline production runtime contract. Existing valid custom URL and TTL
values are retained. Duplicate managed keys, symlinks, world-writable env files,
and invalid HTTPS/TTL fallbacks fail closed before deployment.

After the first preparation, `scripts/immutable_deploy.sh` repeats the migration
idempotently after every fast-forward and before building or switching a release.

## Evidence

A successful deployment writes:

```text
/var/lib/clientplatform/deploy-state/deployment-proof.json
/var/lib/clientplatform/deploy-state/deployed_sha
```

The proof includes:

- exact deployed SHA and release directory;
- dependency lock and SHA-256;
- complete release-tree SHA-256;
- previous SHA, directory, and tree SHA-256;
- production-gate result;
- health and readiness URLs;
- UTC proof time.

`deployed_sha` is written after the proof, never during preflight or rollback.

## Source CI versus live acceptance

Repository tests prove release sealing, atomic symlink switching, rollback ordering, shell syntax, systemd paths, and deploy-gate ordering. They cannot prove the state of the real server, backup artifact, restore database, or deployed systemd process.

Issue `#143` must remain open until a production rollout provides redacted evidence for:

- exact deployed SHA;
- effective systemd `ExecStart` through `current`;
- `current` and `previous` targets;
- successful backup and isolated restore drill;
- previous-release compatibility with the expanded schema;
- successful production gate;
- rollback drill preserving the previous release tree byte-for-byte.

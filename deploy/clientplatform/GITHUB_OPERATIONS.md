# GitHub production operations transport

These workflows operate only on the dedicated ClientPlatform production checkout at
`/opt/clientplatform`:

- `.github/workflows/production-server-topology-probe.yml`
- `.github/workflows/production-deploy-recovery.yml`

They deliberately do **not** expose or call a public deploy webhook. ClientPlatform
`/healthz` and `/readyz` remain loopback-only as required by the production contract.

## Required GitHub Actions secrets

Configure these repository Actions secrets before using either workflow:

- `CLIENTPLATFORM_PRODUCTION_SSH_HOST` — production SSH hostname or IP.
- `CLIENTPLATFORM_PRODUCTION_SSH_USER` — account that owns/can read
  `/opt/clientplatform`.
- `CLIENTPLATFORM_PRODUCTION_SSH_PRIVATE_KEY` — dedicated private key for this
  automation.
- `CLIENTPLATFORM_PRODUCTION_SSH_KNOWN_HOSTS` — pinned OpenSSH known-hosts line(s)
  for the production server.
- `CLIENTPLATFORM_PRODUCTION_SSH_PORT` — optional SSH port; defaults to `22`.

Do not create `known_hosts` with `ssh-keyscan` inside the workflow. Verify the host
key out of band and pin the verified value in
`CLIENTPLATFORM_PRODUCTION_SSH_KNOWN_HOSTS`.

The SSH key should be dedicated to ClientPlatform operations. The topology probe
requires only read access to the repository. Recovery additionally needs permission
to fast-forward the production checkout and run the documented production deploy
command. Prefer a restricted account/sudo rule rather than a general-purpose
administrator key.

## Topology probe

Run **Production server topology probe** manually with `workflow_dispatch`, or push
a `main` commit whose message contains `[probe-server-topology]`.

The probe is read-only and fails closed unless all of these are true:

1. `/opt/clientplatform` is a Git worktree.
2. `origin` is `mailsvb2-bot/clientplatform`.
3. exactly one local branch exists;
4. that branch is `main`;
5. `HEAD` is attached to `main`;
6. tracked files are clean.

A successful run publishes the commit status
`ops/clientplatform-server-single-main`.

## Deploy recovery

`production-deploy-recovery.yml` runs only when a `main` commit message contains
`[recover-production-deploy]`.

Recovery:

1. validates the dedicated ClientPlatform checkout and origin;
2. refuses a dirty tracked worktree or a non-`main` checkout;
3. fetches only `origin/main`;
4. requires the fetched SHA to equal the exact triggering GitHub SHA;
5. fast-forwards with `git merge --ff-only`;
6. runs the canonical `scripts/clientplatform_production_deploy.py` with
   `--recover-unavailable-baseline`.

There is no cross-product webhook fallback. Missing SSH credentials, an unexpected
host key, a wrong repository, a stale SHA, a dirty checkout, or a non-fast-forward
state all fail closed.

## Repair/bootstrap script

On the ClientPlatform production server, `scripts/repair_production_deploy_channel.sh`
can write the dedicated Actions secrets through an already authenticated GitHub CLI.
It no longer installs or repairs any public webhook.

The script intentionally does not generate a privileged SSH key. Supply an existing
dedicated key that is already authorized for the selected production account:

```bash
cd /opt/clientplatform
sudo -E env \
  CLIENTPLATFORM_PRODUCTION_SSH_HOST='verified-host-or-ip' \
  CLIENTPLATFORM_PRODUCTION_SSH_USER='root-or-dedicated-ops-user' \
  CLIENTPLATFORM_PRODUCTION_SSH_PRIVATE_KEY_FILE='/secure/path/to/id_ed25519' \
  bash scripts/repair_production_deploy_channel.sh
```

The script derives the pinned `known_hosts` value from the server's local OpenSSH
host public key instead of trusting an `ssh-keyscan` result. It never prints the
private key. After `REPAIR_OK`, manually dispatch **Production server topology
probe** and require `ops/clientplatform-server-single-main=success`.

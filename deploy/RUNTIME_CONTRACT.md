# ClientPlatform production runtime contract

ClientPlatform production is owned by the dedicated runtime under `deploy/clientplatform/`.
The repository must not expose a second ClientPlatform-branded production installer,
systemd unit, nginx configuration, deploy webhook, or `/root/clientplatform` rollout path.

## Canonical production deployment

Use the artifacts under `deploy/clientplatform/` and the ClientPlatform production
scripts under `scripts/clientplatform_*` only.

Required identity and isolation boundaries include:

- `CLIENTPLATFORM_ENVIRONMENT=production`
- `CLIENTPLATFORM_DEPLOYMENT_ID=clientplatform-production`
- a dedicated ClientPlatform Postgres database
- runtime/state under `/var/lib/clientplatform`
- logs under `/var/log/clientplatform`
- ClientPlatform-owned media/storage buckets and signing secrets
- Telegram polling unless the ClientPlatform production contract is deliberately changed and tested

The canonical production preflight is:

```bash
python -m scripts.clientplatform_production_preflight
```

The production deployment implementation is:

```bash
python -m scripts.clientplatform_production_deploy
```

Operational details, backup/restore requirements, proxying, managed bots and rollout
procedures live in `deploy/clientplatform/` and
`docs/runbooks/CLIENTPLATFORM_PRODUCTION_ISOLATION.md`.

## Legacy compatibility is application-only

Some `CLIENTPLATFORM_*` environment names remain supported as compatibility fallbacks inside
the application while migration is in progress. They do **not** authorize a separate
ClientPlatform production deployment inside this repository. ClientPlatform-prefixed
settings win when both are present.

In particular, production tooling in this repository must not reintroduce:

- `deploy/clientplatform.service`
- `deploy/clientplatform.env.example`
- `deploy/nginx-clientplatform.conf`
- root-level legacy `deploy/deploy.sh` or `deploy/install_server.sh`
- `ops/deploy_webhook.py` or `ops/deploy_webhook_hardened.py`
- a service or script whose operational root is `/root/clientplatform`

## Runtime state

Production state must stay outside the repository and inside ClientPlatform-owned
locations. Do not write databases, logs, secrets, deploy evidence or backups into
the checked-out source tree.

## Regression guard

`tests/test_clientplatform_runtime_ownership.py` enforces the repository-level
ownership boundary. The more detailed environment, database, storage, backup and
network invariants remain covered by `tests/test_clientplatform_production_isolation.py`.

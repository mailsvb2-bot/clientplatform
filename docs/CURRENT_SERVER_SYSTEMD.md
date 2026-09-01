# Production systemd contract

This file preserves the known legacy Timeweb unit and defines the immutable
runtime contract that must replace it during the next verified production
rollout.

## Legacy evidence before immutable rollout

The previously observed server unit executed the mutable source checkout:

```ini
[Service]
WorkingDirectory=/root/clientplatform
EnvironmentFile=/etc/clientplatform/clientplatform.env
ExecStart=/root/clientplatform/.venv/bin/python /root/clientplatform/main.py
User=root
```

That shape is retained here only as migration evidence. It is not an acceptable
post-rollout production invariant because dependency installation and rollback
mutate one shared `.venv`.

## Required effective unit after rollout

`scripts/immutable_deploy.sh` installs:

```text
/etc/systemd/system/clientplatform.service.d/immutable-release.conf
```

with this runtime boundary:

```ini
[Service]
WorkingDirectory=/var/lib/clientplatform/runtime/current
ExecStart=
ExecStart=/var/lib/clientplatform/runtime/current/.venv/bin/python /var/lib/clientplatform/runtime/current/main.py
Environment=PYTHONDONTWRITEBYTECODE=1
```

The existing production environment-file override remains authoritative:

```ini
EnvironmentFile=/etc/clientplatform/clientplatform.env
Environment=MPLCONFIGDIR=/var/lib/clientplatform/mplcache
```

The repository template `deploy/clientplatform.service` uses the same immutable
`current` path.

## Required post-rollout evidence

```bash
sudo systemctl daemon-reload
sudo systemctl restart clientplatform.service
sudo systemctl show clientplatform.service \
  -p WorkingDirectory \
  -p ExecStart \
  -p EnvironmentFiles
readlink -f /var/lib/clientplatform/runtime/current
readlink -f /var/lib/clientplatform/runtime/previous
cat /var/lib/clientplatform/deploy-state/deployment-proof.json
cat /var/lib/clientplatform/deploy-state/deployed_sha
curl -fsS http://127.0.0.1:8082/healthz
curl -fsS http://127.0.0.1:8082/readyz
```

Expected proof:

- `WorkingDirectory=/var/lib/clientplatform/runtime/current`;
- `ExecStart` uses `current/.venv/bin/python` and `current/main.py`;
- `current` resolves to the exact deployed Git SHA;
- `previous` resolves to the independently sealed rollback SHA;
- `deployment-proof.json` records both release-tree hashes and `PRODUCTION_GATE_OK`;
- `deployed_sha` equals the `current` release and was written after the proof;
- `/healthz` and `/readyz` return HTTP 200;
- readiness has database, schema, scheduler, webhook, messenger outbox, and payment retry checks green.

## Production invariants after acceptance

- Source/control checkout: `/root/clientplatform`;
- Runtime code: `/var/lib/clientplatform/runtime/current`;
- Immutable releases: `/var/lib/clientplatform/runtime/releases/<sha>`;
- Shared licensed audio: `/var/lib/clientplatform/audio` unless explicitly overridden;
- Environment file: `/etc/clientplatform/clientplatform.env`;
- Health runtime: `127.0.0.1:8082`;
- Messenger HTTP ingress: `127.0.0.1:8081`;
- Telegram transport: polling;
- Database engine: PostgreSQL;
- Matplotlib cache: `/var/lib/clientplatform/mplcache`.

The legacy `/root/clientplatform/.venv` path must not appear in the effective
post-rollout `ExecStart`. Issue `#143` remains open until this effective-unit,
backup, restore-drill, exact-SHA, and rollback evidence is captured from the real
server.

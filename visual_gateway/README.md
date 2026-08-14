# Visual Creative Gateway v1

This directory is the versioned ClientPlatform-facing gateway artifact required by the Visual Creative Studio contract.
It is deliberately provider-neutral: generation/provider operations are delegated to the existing provider gateway through `VISUAL_GATEWAY_UPSTREAM_URL`, while this layer owns the application-facing capability version, cost guard, durable render-pack state, tenant isolation, deterministic composition and artifact digests.

## Runtime contract

Authenticated endpoints:

- `GET /v1/capabilities`
- `POST /v1/creative/generations` (quota-guarded provider-gateway delegation)
- `GET /v1/creative/generations/{id}` and `/content`
- `GET /v1/providers`, `GET /v1/usage`
- `POST /v1/creative/render-packs`
- `GET /v1/creative/render-packs/{id}`
- `GET /v1/creative/render-packs/{id}/content/{format}`

`/v1/capabilities` never contacts the provider gateway. Render packs are keyed by the exact source job, tenant scope, ordered format set and canonical composition. A caller idempotency key is separately bound to that fingerprint; reusing it for a different request returns HTTP 409.

Render state and generated assets are stored under `VISUAL_GATEWAY_STATE_DIR` in SQLite + durable files. A pack is marked succeeded only after files have been fsync'd and their SHA-256 values are stored transactionally.

## Required environment

- `VISUAL_GATEWAY_TOKEN`: bearer token expected from ClientPlatform.
- `VISUAL_GATEWAY_UPSTREAM_URL`: current provider-neutral generation gateway base URL.
- `VISUAL_GATEWAY_UPSTREAM_TOKEN`: optional bearer token for that upstream.
- `VISUAL_GATEWAY_STATE_DIR`: durable volume path, default `/var/lib/visual-gateway`.
- `VISUAL_GATEWAY_DAILY_GENERATION_LIMIT`: per-scope UTC-day pre-egress guard, default `100`.

Secrets are runtime-only; do not bake them into the image or repository.

## Build provenance

Build from the repository root and label the image with the exact commit:

```bash
docker build -f visual_gateway/Dockerfile --build-arg VCS_REF="$(git rev-parse HEAD)" -t clientplatform-visual-gateway:"$(git rev-parse --short HEAD)" .
```

Production deployment and a paid YandexART smoke are intentionally separate authorization gates; this source change does not perform either action.

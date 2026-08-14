# ClientPlatform Visual Provider Gateway

This package is the **provider-adapter boundary** behind ClientPlatform's versioned `visual_gateway` contract. It is not a second application brain and does not own offers, audiences, campaigns, entitlements, billing truth, or any other ClientPlatform business state.

Production flow:

```text
ClientPlatform
  -> visual_gateway (versioned application contract + render packs + ClientPlatform quota boundary)
  -> visual-provider-gateway (this package: provider routing/idempotency/provider job lifecycle)
  -> YandexART / GigaChat / OpenAI / Runway / operator-owned self-hosted worker
```

Provider credentials, model identifiers, regional routing and provider-transport details remain isolated here so provider volatility does not leak into ClientPlatform business logic. The durable SQLite registry reserves an idempotency key **before provider I/O**, preventing a retry after an ambiguous timeout from starting a second paid generation.

## Production safety

- Provider failover after an attempted provider POST is disabled by default because the first provider may already have accepted and billed an ambiguous request.
- Request-level provider override is disabled by default and cannot escape the deployment country policy.
- Provider errors are reduced to bounded, secret-safe diagnostic codes such as `visual_provider_submit_http_400`; provider response bodies and credentials are never exposed through the public job contract.
- Per-client, per-kind and active-job ceilings are operational spend/load guardrails, not billing-system truth.
- Remote reference URLs are denied unless the operator explicitly enables and allowlists them.

## ClientPlatform production defaults

The canonical production Compose topology supplies the ClientPlatform principal token and mounts the existing durable provider-gateway data directory so idempotency/job history survives migration from the legacy standalone container.

Relevant environment variables include:

```env
VISUAL_CREATIVE_ENABLED=1
VISUAL_DEPLOYMENT_COUNTRY=RU
VISUAL_GATEWAY_CLIENT_TOKENS_JSON={"clientplatform":"<same upstream token used by the canonical wrapper>"}
VISUAL_GATEWAY_DB=/data/visual_gateway.sqlite3
VISUAL_CREATIVE_OUTPUT_DIR=/data/output

YANDEX_ART_BASE_URL=https://llm.api.cloud.yandex.net:443
YANDEX_ART_FOLDER_ID=...
YANDEX_API_KEY=...
# or YANDEX_ART_IAM_TOKEN=...
```

RU image routing defaults to `yandexart,gigachat,selfhosted`; RU video routing defaults to `selfhosted`. International provider routes remain opt-in/operator-configured.

## API

Authenticated endpoints used by the canonical wrapper:

- `GET /v1/providers`
- `GET /v1/usage`
- `POST /v1/creative/generations`
- `GET /v1/creative/generations/{id}?scope_id=...`
- `GET /v1/creative/generations/{id}/content?scope_id=...`

`GET /healthz` is unauthenticated and intended only for the private container health check.

## Tests

From the repository root:

```bash
python -m pip install -r visual_provider_gateway/requirements-dev.txt
PYTHONPATH=. python -m pytest -q visual_provider_gateway/tests
```

The tests cover authentication, scope/client isolation, pre-egress idempotency, quota ceilings, provider routing policy, fail-closed behavior after ambiguous submits, bounded media handling and safe provider diagnostics.

See `SELF_HOSTED_WORKER_CONTRACT.md` for the normalized operator-owned GPU worker contract.

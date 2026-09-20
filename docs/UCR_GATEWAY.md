# Universal Communication Runtime gateway boundary

Status: owner-directed integration boundary for `mailsvb2-bot/Universal-Communication-Runtime`.

Pinned UCR revision: `22d598e057769d59fe0aa47e169cf2eb904abb18`.

## Why this boundary exists

ClientPlatform owns tenant isolation, customers, CRM state, automations, consent, billing, attribution, provider credentials and the existing Telegram/VK/MAX/email/SMS/web-chat product behavior. UCR owns provider-independent communication-runtime primitives behind its public versioned services.

The integration must not create a second CRM, automation engine, provider-delivery owner or shadow UCR model inside ClientPlatform. The projects therefore connect through a separately deployed thin gateway. ClientPlatform knows only the gateway URL, an operator-managed secret reference and the exact allowed UCR revision. The UCR repository remains independently developed and is not copied or modified by this integration.

The gateway is a transport adapter, not a semantic adapter: it may translate the HTTP envelope below to the pinned UCR gRPC bindings, but it must not invent Device, Group, membership, Conversation, Message, Intent or Call state that was not present in the canonical UCR request.

## Configuration

The adapter is disabled by default.

- `CLIENTPLATFORM_UCR_GATEWAY_ENABLED=true` enables calls.
- `CLIENTPLATFORM_UCR_GATEWAY_URL=https://...` selects the gateway. Plain HTTP is accepted only for loopback (`localhost`, `127.0.0.1`, `::1`). Credentials, query strings and fragments are rejected in the URL.
- `CLIENTPLATFORM_UCR_GATEWAY_TOKEN_REFERENCE=secret://env/CLIENTPLATFORM_SECRET_UCR_GATEWAY_TOKEN` selects the credential reference. A raw token is never valid configuration.
- `CLIENTPLATFORM_UCR_GATEWAY_TIMEOUT_SEC` defaults to `5.0` and is bounded to 0.25..30 seconds.
- `CLIENTPLATFORM_UCR_GATEWAY_MAX_RESPONSE_BYTES` defaults to 131072 and is bounded to 1 KiB..1 MiB.

The default secret value is expected in `CLIENTPLATFORM_SECRET_UCR_GATEWAY_TOKEN`; only its `secret://env/...` reference belongs in configuration or persisted records.

## Revision contract

Every successful response includes the exact pinned revision:

```json
{
  "ok": true,
  "ucr_revision": "22d598e057769d59fe0aa47e169cf2eb904abb18"
}
```

A mismatch fails closed so an unreviewed UCR upgrade cannot silently change ClientPlatform communication semantics.

Requests authenticate with `Authorization: Bearer <resolved secret>` and send `X-ClientPlatform-UCR-Revision` with the pinned revision. Mutating RPCs additionally require a gateway `Idempotency-Key`. The key is a mandatory stable retry-correlation guard at the ClientPlatform HTTP boundary; the sidecar does not claim durable deduplication from that header. Callers must reuse canonical UCR request IDs for retries, and UCR remains the owner of duplicate/conflict semantics.

## Health

`GET /v1/health`

This verifies gateway reachability and revision compatibility. Health is not proof that a communication effect completed.

## RPC transport envelope

All UCR operations use one bounded transport endpoint:

`POST /v1/rpc`

```json
{
  "version": 1,
  "service": "ucr.v1.IntegrationService",
  "method": "CreateConversation",
  "request": {
    "conversation": {
      "...": "exact canonical request fields"
    }
  }
}
```

The `request` object is the JSON representation of the pinned UCR protobuf request. ClientPlatform does not maintain a second copy of those schemas and does not reinterpret their business meaning. The request envelope is capped at 64 KiB; the canonical UCR message itself remains subject to UCR's stricter/per-field limits.

A successful RPC response must echo the exact service and method and provide a JSON object result:

```json
{
  "ok": true,
  "ucr_revision": "22d598e057769d59fe0aa47e169cf2eb904abb18",
  "service": "ucr.v1.IntegrationService",
  "method": "CreateConversation",
  "result": {
    "...": "canonical UCR response"
  }
}
```

Service/method mismatch or a missing object result is a protocol failure. A canonical UCR protobuf response carrying its `error` oneof is never promoted to `ok: true`; the prepared sidecar maps that error envelope to a bounded HTTP rejection/unavailability result without echoing UCR diagnostics.

## Allowed public UCR surface

The ClientPlatform adapter whitelists only methods that exist in the pinned UCR public protobuf services.

### `ucr.v1.IntegrationService`

Mutating operations, therefore requiring a gateway idempotency key:

- `SubmitCommand`
- `CreateIdentity`
- `LinkIdentity`
- `CreateConversation`
- `SendMessage`
- `CreateCommunicationIntent`

Read operations:

- `GetIdentity`
- `ResolveIdentityBinding`
- `GetConversation`
- `GetMessage`
- `GetCommunicationIntent`

### `ucr.v1.CallService`

Mutating operations:

- `StartCall`
- `SignalCall`

Read operation:

- `GetCall`

### `ucr.v1.UniversalConferenceService`

This pin exposes only the two reviewed read-only methods needed for the first UIII attendance consumer slice:

- `GetParticipantAttendance`
- `GetCapabilities`

Conference creation, participant mutation, runtime preparation and join-grant mutation remain outside the ClientPlatform gateway in this slice. The adapter does not infer or synthesize those operations.

`StartCall` receives the canonical UCR request supplied by the caller. ClientPlatform does not turn a short `(tenant, conversation, participants)` tuple into a fabricated `CallSession` because UCR owns the exact call model and its required authority/revision fields.

## Explicitly not exposed

The gateway contract does **not** expose synthetic operations such as:

- `EnsureCommunicationContext`
- `RegisterDevice`
- `CreateProtectedGroup`
- `SetGroupMembership`

The pinned `IntegrationService` does not publish those operations. If a future UCR revision exposes additional public methods, ClientPlatform may add them only together with an exact UCR revision update, contract review and regression evidence. Reaching into UCR internal stores/core APIs to simulate a missing public RPC is forbidden by this boundary.

## Data ownership and minimization

ClientPlatform remains authoritative for business/customer relationships, tenant/RBAC, consent, automation policy, money and provider credentials. UCR request payloads may contain the communication data required by the chosen canonical RPC, but ClientPlatform must not copy unrelated profile data, provider tokens, billing secrets or internal database records merely for routing.

A successful UCR acknowledgement proves only what the corresponding UCR public response claims. It must not be promoted into provider delivery, read, media establishment, payment or business-outcome evidence unless the canonical owner for that evidence confirms it.

## Failure semantics

The boundary is deliberately fail-closed:

- disabled configuration performs no secret resolution and no network I/O;
- missing/short/unresolvable credentials stop the call before network I/O;
- non-HTTPS external URLs are rejected;
- unknown/non-whitelisted RPC methods are rejected before network I/O;
- mutating RPCs without a valid idempotency key are rejected before network I/O;
- read RPCs cannot masquerade as mutations by attaching an idempotency key;
- request JSON rejects NaN/infinity/non-serializable values and is capped at 64 KiB;
- responses are capped by configuration;
- 401/403/409/422 become explicit rejections;
- 429 and 5xx become temporary unavailability;
- canonical UCR `ErrorEnvelope` responses fail closed instead of becoming transport success;
- invalid JSON, unexpected success shapes, service/method mismatch and revision drift become protocol failures;
- gateway error details are not echoed into ClientPlatform exceptions, preventing accidental secret leakage.

UCR availability is optional. When the feature flag is off, all existing ClientPlatform channel behavior stays unchanged.

## Deployment boundary

`ucr_gateway_sidecar/` now contains a prepared executable HTTP→gRPC adapter. Its container build fetches the exact pinned UCR commit, verifies the fetched SHA and generates Python bindings from that revision's public protobuf files instead of vendoring another schema copy into ClientPlatform. The sidecar authenticates upstream with UCR's canonical binary Service Principal metadata and requires TLS for non-loopback gRPC targets.

Prepared does not mean deployed. The sidecar is not wired into `deploy/clientplatform/compose.production.yml`, the ClientPlatform feature flag remains disabled by default, and this repository still does not create a production UCR listener. A production UCR listener, durable UCR storage, Service Principal grants/quota, TLS/network policy and rollout evidence remain a separate deployment slice requiring an explicit owner decision. This document does not authorize production deployment.

## Upgrade rule

Do not point this adapter at `main`, a branch name or an unreviewed UCR build. A UCR upgrade requires a deliberate change of `UCR_PINNED_REVISION`, review of the public protobuf diff, regression tests and green ClientPlatform CI. This keeps the integration additive and prevents runtime drift from becoming a hidden production dependency.

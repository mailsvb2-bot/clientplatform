# Universal Communication Runtime gateway boundary

Status: owner-directed integration boundary for `mailsvb2-bot/Universal-Communication-Runtime`.

Pinned UCR revision: `8097b41e69634c944c225f7071e80b991d4ddc02`.

## Why this boundary exists

ClientPlatform owns tenant isolation, customers, CRM state, automations, consent, billing, attribution, provider credentials and the existing Telegram/VK/MAX/email/SMS/web-chat product behavior. UCR owns canonical communication-runtime primitives such as identities, devices, protected groups/conversations and call sessions. The integration must not create a second CRM, automation engine or provider-delivery owner inside ClientPlatform.

The projects therefore connect through a separately deployed UCR gateway. ClientPlatform knows only the gateway URL, an operator-managed secret reference and the exact allowed UCR revision. The UCR repository remains independently developed and is not copied or modified by this integration.

## Configuration

The adapter is disabled by default.

- `CLIENTPLATFORM_UCR_GATEWAY_ENABLED=true` enables calls.
- `CLIENTPLATFORM_UCR_GATEWAY_URL=https://...` selects the gateway. Plain HTTP is accepted only for loopback (`localhost`, `127.0.0.1`, `::1`). Credentials, query strings and fragments are rejected in the URL.
- `CLIENTPLATFORM_UCR_GATEWAY_TOKEN_REFERENCE=secret://env/CLIENTPLATFORM_SECRET_UCR_GATEWAY_TOKEN` selects the credential reference. A raw token is never valid configuration.
- `CLIENTPLATFORM_UCR_GATEWAY_TIMEOUT_SEC` defaults to `5.0` and is bounded to 0.25..30 seconds.
- `CLIENTPLATFORM_UCR_GATEWAY_MAX_RESPONSE_BYTES` defaults to 131072 and is bounded to 1 KiB..1 MiB.

The default secret value is expected in `CLIENTPLATFORM_SECRET_UCR_GATEWAY_TOKEN`; only its `secret://env/...` reference belongs in configuration or persisted records.

## Contract

Every successful response is a JSON object containing:

```json
{
  "ok": true,
  "ucr_revision": "8097b41e69634c944c225f7071e80b991d4ddc02"
}
```

The revision must match the repository pin exactly. A mismatch fails closed so an unreviewed UCR upgrade cannot silently change ClientPlatform communication semantics.

Requests authenticate with `Authorization: Bearer <resolved secret>` and send `X-ClientPlatform-UCR-Revision` with the pinned revision. Mutating requests also require an `Idempotency-Key`. ClientPlatform opaque IDs are used as boundary references; the adapter does not send customer names, phone numbers, message-provider credentials or other raw profile data.

### Health

`GET /v1/health`

This is the only non-mutating operation in the first boundary slice. It verifies gateway reachability and revision compatibility.

### Ensure communication context

`POST /v1/communication-contexts`

```json
{
  "version": 1,
  "tenant_key": "business:<opaque-id>",
  "identity_key": "customer:<opaque-id>",
  "device_key": "clientplatform:<opaque-device-id>",
  "group_key": "conversation:<opaque-id>",
  "member_identity_keys": ["customer:<opaque-id>", "staff:<opaque-id>"]
}
```

The gateway maps this bounded, retry-safe request into the UCR-owned Identity -> Device -> protected Group/Conversation -> atomic membership workflow. Canonically equal retries must return the same context; conflicting reuse of an idempotency key must fail rather than create duplicate UCR state.

### Start call

`POST /v1/calls`

```json
{
  "version": 1,
  "tenant_key": "business:<opaque-id>",
  "context_key": "conversation:<opaque-id>",
  "participant_identity_keys": ["customer:<opaque-id>", "staff:<opaque-id>"]
}
```

The gateway maps the request to UCR `CallSession` semantics. At least two unique participants are required. The ClientPlatform adapter does not claim media establishment or provider delivery from a mere accepted call-session response.

## Failure semantics

The boundary is deliberately fail-closed:

- missing/short/unresolvable credentials stop the call before network I/O;
- non-HTTPS external URLs are rejected;
- request payloads are capped at 64 KiB and responses are capped by configuration;
- 401/403/409/422 become explicit rejections;
- 429 and 5xx become temporary unavailability;
- invalid JSON, unexpected success shapes and revision drift become protocol failures;
- gateway error details are not echoed into ClientPlatform exceptions, preventing accidental secret leakage.

UCR availability is optional. When the feature flag is off, all existing ClientPlatform channel behavior stays unchanged.

## Upgrade rule

Do not point this adapter at `main`, a branch name or an unreviewed UCR build. A UCR upgrade requires a deliberate change of `UCR_PINNED_REVISION`, contract review, regression tests and green ClientPlatform CI. This keeps the integration additive and prevents runtime drift from becoming a hidden production dependency.

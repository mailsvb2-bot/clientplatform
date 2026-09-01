# ADR-0006: Signed media gateway and explicit clientplatform runtime lifecycle

- Status: accepted for additive clientplatform runtime
- Date: 2026-07-28
- Scope: private lesson media and process lifecycle

## Context

The Telegram HTTP runtime from ADR-0005 can send Telegram file IDs and public
HTTPS URLs. Program lessons, however, already use logical private references
such as `s3://bucket/key`. Passing those values directly to Telegram either
fails or encourages making private storage public.

The runtime also needs a clear composition boundary. Implementing a scheduler
class is not enough: the application must have one explicit start/stop/health
surface, while remaining disabled until a later production rollout.

## Decision

### Private media resolution

Transport adapters receive a `MediaReferenceResolver`.

- Telegram file IDs pass through unchanged.
- Public media references must use HTTPS.
- `http://`, FTP, URL credentials, fragments and control characters fail
  closed.
- `s3://bucket/key` is parsed and normalized before signing.
- invalid buckets, empty path components, `.`/`..`, query strings and control
  characters are rejected.

### HMAC media gateway

`HmacMediaGatewayResolver` converts a private storage reference to:

`https://<gateway>/media/<bucket>/<key>?expires=<unix>&sig=<hmac>`

The signature covers:

`GET\n/path\nexpiry`

The signing key is resolved from an existing `CredentialProvider` for each
send. It is never stored in the dispatch, resolved URL, logs or database.
Signed URLs live for 60–900 seconds. The gateway URL must be HTTPS and cannot
contain credentials, query data or fragments.

This change creates the signing side and contract. The HTTP media-gateway that
validates the signature and streams the object remains a separate deployable
boundary.

### In-memory only

The signed URL is created after the dispatch lease is claimed and immediately
before the provider call. It is not written back to `LessonDelivery`, outbox or
progress records. Persistent state retains only the logical `s3://` reference.

### Retry semantics

Provider and media errors may expose a `retryable` attribute.

- transient network, 429 and 5xx failures retain the configured retry budget;
- terminal 4xx, unsafe media references and unresolved private schemes are
  dead-lettered immediately;
- secret values remain redacted before persistence.

### Explicit lifecycle

clientplatform exposes:

- `start_clientplatform_runtime()`;
- `stop_clientplatform_runtime()`;
- `clientplatform_runtime_health_snapshot()`.

The lifecycle owns at most one `ClientPlatformDispatchScheduler`, awaits shutdown and is
safe across separate event loops. It is not called automatically by imported
ClientPlatform startup in this change.

## Consequences

Positive:

- private object storage does not need public ACLs;
- Telegram receives only short-lived HTTPS references;
- traversal and malformed references fail before secret lookup or network I/O;
- terminal errors stop wasting retries;
- future application composition has one explicit lifecycle surface;
- production behavior remains unchanged by default.

Trade-offs:

- a validating media-gateway service is still required before real `s3://`
  delivery;
- signed URL possession grants temporary read access until expiry;
- key rotation and gateway deployment policy remain operational work;
- the runtime is observable but still intentionally not wired into startup.

## Required regression evidence

- deterministic signature and bounded expiry;
- no secret in signed URL;
- file ID and HTTPS passthrough without secret lookup;
- invalid bucket/path/traversal rejection;
- base URL and TTL validation;
- Telegram adapter sees only the resolved HTTPS URL;
- terminal errors use one attempt, transient errors retain the budget;
- lifecycle is explicit, single-owner, stoppable and observable.

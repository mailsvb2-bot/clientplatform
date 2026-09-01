# ADR-0005: Telegram HTTP runtime and bounded dispatch owner

- Status: accepted for additive clientplatform runtime
- Date: 2026-07-28
- Scope: clientplatform outbound Telegram delivery

## Context

ADR-0004 introduced tenant-scoped connections and a transport-neutral dispatch
outbox. It intentionally stopped before real provider I/O. The next boundary
must resolve credentials at send time, call the Telegram Bot API without
leaking the token, and own repeated dispatch work without overlapping batches.

The imported technical baseline already had a process-wide TaskManager and a
large scheduler. clientplatform must not silently join that production loop before its own
runtime is independently testable and explicitly enabled.

## Decision

### Secret resolution

The first provider supports only references shaped as:

`secret://env/CLIENTPLATFORM_SECRET_*`

The environment variable namespace is restricted. Raw credentials, arbitrary
environment variables and unresolved vault/KMS references are rejected with
safe errors that contain no secret value.

Environment-backed resolution is a bootstrap provider, not a claim that an
environment variable is the final long-term secret manager. The
`CredentialProvider` boundary remains compatible with later KMS/Vault-backed
implementations.

### Telegram Bot API client

clientplatform uses a small `aiohttp` client behind the existing `TelegramBotClient`
protocol. It:

- uses HTTPS for the Bot API base URL;
- disables redirects;
- bounds request time;
- returns only the provider `message_id`;
- classifies 429 and 5xx responses as retryable;
- converts provider/network failures to sanitized error codes;
- never propagates the token-bearing request URL or Telegram description.

Text is sent with `sendMessage`. Audio, video, documents and photos use their
corresponding Bot API methods. Media values may be Telegram `file_id` strings
or HTTP(S) URLs. Private logical references such as `s3://...` are rejected
before network I/O; a signed media resolver will be a separate boundary.

### Runtime configuration

The runtime is disabled by default:

`CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED=0`

Batch size, interval, tick timeout, per-request timeout, retry count and lease
TTL are independently bounded by configuration.

### Scheduler ownership

`ClientPlatformDispatchScheduler` is a single-owner serial loop:

- `start()` is idempotent;
- a second overlapping owner is refused;
- one bounded tick completes before the next begins;
- cancellation is explicit and awaited;
- timeout and safe failure counters are visible in a health snapshot;
- tasks are created through the canonical process TaskManager.

The owner is implemented and tested but is not wired into the imported
baseline startup in this change. Activation requires an explicit later
runtime composition step and the feature flag.

## Consequences

Positive:

- real Bot API I/O exists behind a deterministic test seam;
- tokens remain outside persistence and persisted errors;
- the dispatch worker cannot overlap itself through this owner;
- production remains unaffected by default;
- private storage references cannot accidentally be exposed to Telegram.

Trade-offs:

- one HTTP session is currently created per API call;
- only Telegram is composed in the first runtime;
- `s3://` content needs a signed URL or upload resolver before real media
  delivery;
- environment-backed secret resolution is suitable for controlled bootstrap,
  not the final managed-secret implementation.

## Required regression evidence

- default runtime is disabled;
- environment namespace and missing references fail closed;
- secret material never appears in an error;
- Telegram methods route to the correct HTTPS endpoint;
- 429 is retryable and 401 is terminal;
- private `s3://` media is rejected before HTTP;
- scheduler has one owner, bounded timeout and health counters;
- disabled scheduler creates no background task.

# ClientPlatform UCR HTTP→gRPC sidecar

This component is the executable transport adapter for the optional ClientPlatform ↔ Universal Communication Runtime boundary.

It is **not** a second communication runtime, CRM, identity model or authorization owner. It accepts the bounded ClientPlatform HTTP envelope documented in `docs/UCR_GATEWAY.md`, validates it, then forwards the exact request JSON through the public pinned UCR protobuf/gRPC services.

Pinned UCR revision: `22d598e057769d59fe0aa47e169cf2eb904abb18`.

## Build contract

The Docker build fetches that exact immutable UCR commit and generates Python gRPC bindings from `proto/ucr/v1/*.proto`. ClientPlatform does not vendor a second copy of the UCR protobuf schemas and the build fails if the fetched commit does not equal the pin.

Runtime dependencies are isolated under this component. The ordinary ClientPlatform Python process does not import `grpcio`, protobuf generated modules, or UCR code.

## HTTP surface

- `GET /v1/health`
- `POST /v1/rpc`

Both endpoints require:

- `Authorization: Bearer <CLIENTPLATFORM_SECRET_UCR_GATEWAY_TOKEN>`
- `X-ClientPlatform-UCR-Revision: 22d598e057769d59fe0aa47e169cf2eb904abb18`

Mutating methods additionally require the validated `Idempotency-Key` already required by the ClientPlatform caller boundary. The sidecar does not invent semantic state from that key: canonical UCR IDs and UCR's durable owners remain authoritative for duplicate/conflict behavior.

Only the reviewed public method whitelist from `ucr.v1.IntegrationService`, `ucr.v1.CallService` and the read-only `ucr.v1.UniversalConferenceService` attendance/capability surface in `docs/UCR_GATEWAY.md` is callable. Unknown services and methods are rejected before gRPC I/O.

## UCR Service Principal

The outgoing UCR call uses UCR's binding-specific binary metadata keys:

- `ucr-service-credential-id-bin`
- `ucr-service-credential-secret-bin`

Required runtime configuration:

- `CLIENTPLATFORM_SECRET_UCR_GATEWAY_TOKEN` — HTTP client token, at least 32 UTF-8 bytes.
- `CLIENTPLATFORM_UCR_GRPC_TARGET` — exact `host:port` of the separately deployed UCR public gRPC listener.
- `CLIENTPLATFORM_UCR_GRPC_TLS_ENABLED` — defaults to `1`; plaintext gRPC is rejected outside loopback.
- `CLIENTPLATFORM_UCR_SERVICE_CREDENTIAL_ID` — canonical UCR service credential opaque ID.
- `CLIENTPLATFORM_SECRET_UCR_SERVICE_CREDENTIAL_B64` — base64 encoding of the exact 32-byte UCR Service Principal secret.
- `CLIENTPLATFORM_UCR_GRPC_ROOT_CA_FILE` — optional reviewed CA bundle for the UCR endpoint.
- `CLIENTPLATFORM_UCR_GRPC_TIMEOUT_SEC` — 0.25..30 seconds, default 5.
- `CLIENTPLATFORM_UCR_GATEWAY_BIND_HOST` / `CLIENTPLATFORM_UCR_GATEWAY_BIND_PORT` — default `127.0.0.1:8098`.

No Service Principal permission grants or quota policy are fabricated here. They must be provisioned by the canonical UCR administrative owner. Missing/invalid UCR credentials, quota or authorization therefore fail closed through the UCR public API.

## Security properties

The sidecar:

- validates the exact UCR revision before upstream work;
- compares the ClientPlatform bearer token with constant-time comparison;
- caps the HTTP request at 64 KiB and response at 1 MiB;
- rejects NaN/infinity and unknown RPC methods;
- rejects read calls carrying mutation idempotency metadata;
- maps gRPC failures to bounded error codes without returning upstream details;
- requires TLS for non-loopback gRPC targets;
- does not log request bodies or credential values;
- runs as a non-root user in the container.

## Deployment status

Prepared only. This change does **not** add the sidecar to `deploy/clientplatform/compose.production.yml`, does not enable `CLIENTPLATFORM_UCR_GATEWAY_ENABLED`, and does not create a UCR production listener. Production activation remains a separate reviewed rollout after the independently deployed UCR endpoint, Service Principal grants/quotas and TLS/network policy are proven.

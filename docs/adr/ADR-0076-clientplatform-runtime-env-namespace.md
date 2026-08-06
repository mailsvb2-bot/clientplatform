# ADR-0076: ClientPlatform runtime environment namespace

**Status:** Accepted  
**Date:** 2026-08-07

## Context

ClientPlatform was created from an isolated Metrotherapy baseline. The database and writable-path bootstrap still accepted only these inherited names:

```text
METRO_DATA_DIR
METRO_LOGS_DIR
METRO_DB_ENGINE
METRO_DB_PATH
```

The ClientPlatform canon explicitly forbids `METRO_*` as the final production namespace. Keeping the inherited names as the only contract also makes deployments ambiguous: an operator cannot tell whether a variable belongs to ClientPlatform or to the original product.

A hard rename in one release would be unsafe because existing test, development and production-like environments may still provide the legacy names.

## Decision

ClientPlatform introduces and prefers the following product-owned variables:

```text
CLIENTPLATFORM_DATA_DIR
CLIENTPLATFORM_LOGS_DIR
CLIENTPLATFORM_DB_ENGINE
CLIENTPLATFORM_DB_PATH
```

Resolution order is:

1. non-empty `CLIENTPLATFORM_*` value;
2. corresponding non-empty `METRO_*` value as a temporary compatibility fallback;
3. the existing calculated default.

When both namespaces are present, the ClientPlatform value always wins.

`DATABASE_URL` remains unchanged because it is a standard deployment contract rather than a Metrotherapy-specific name.

The canonical all-user scenario gate must use only the ClientPlatform database variables. Legacy fallback remains covered by a dedicated regression test but must not be copied into new deployment examples or workflows.

## Consequences

### Positive

- new ClientPlatform deployments no longer require Metrotherapy-branded runtime variables;
- existing environments continue to start during the migration window;
- mixed environments resolve deterministically in favour of ClientPlatform;
- test and production documentation can migrate incrementally without a flag day.

### Costs

- both names remain supported temporarily;
- the fallback must be removed in a later explicitly announced breaking migration;
- operators should avoid setting both namespaces after confirming the new values work.

## Security properties

- no secret value is logged or copied during resolution;
- path resolution keeps the existing absolute-path behaviour for explicit data and log directories;
- an inherited `METRO_*` value cannot override an explicit `CLIENTPLATFORM_*` value;
- the change does not alter `DATABASE_URL`, credentials, network access or production data.

## Migration

1. Add the equivalent `CLIENTPLATFORM_*` variables to each environment.
2. Deploy while leaving legacy variables in place.
3. Verify resolved database engine and writable paths through existing preflight and health checks.
4. Remove the legacy variables from that environment.
5. After all supported environments have migrated, remove the fallback in a separate ADR and release.

## Verification

Regression tests must prove:

- ClientPlatform variables win when both namespaces are configured;
- legacy-only environments still resolve identically;
- the canonical scenario gate contains no `METRO_DB_ENGINE` or `METRO_DB_PATH` contract;
- database-driver guidance uses ClientPlatform examples;
- `core/paths.py` and `services/db/runtime.py` remain inside the critical typing and security manifests.

# ADR-0076: ClientPlatform runtime environment namespace

**Status:** Accepted
**Date:** 2026-08-07
**Updated:** 2026-08-31

## Context

The imported baseline originally carried a product-specific runtime namespace. A transitional compatibility period allowed ClientPlatform-owned names to coexist with inherited aliases. That migration is complete. Keeping inherited aliases now creates ambiguity and makes it possible for a foreign runtime contract to re-enter production.

## Decision

ClientPlatform accepts only ClientPlatform-owned runtime names for product-specific configuration. Canonical path and database variables include:

```text
CLIENTPLATFORM_RUNTIME_ROOT
CLIENTPLATFORM_WRITABLE_ROOT
CLIENTPLATFORM_DATA_DIR
CLIENTPLATFORM_LOGS_DIR
CLIENTPLATFORM_DB_ENGINE
CLIENTPLATFORM_DB_PATH
```

`DATABASE_URL` remains unchanged because it is a standard database deployment contract rather than a product-specific alias.

There is no inherited namespace fallback. Production preflight fails closed when required ClientPlatform variables are missing.

## Consequences

- production configuration is unambiguous;
- runtime paths are visibly owned by ClientPlatform;
- copied product configuration cannot silently override ClientPlatform settings;
- environments that still relied on transitional aliases must migrate before deployment.

## Migration

1. Set the corresponding `CLIENTPLATFORM_*` variables.
2. Verify production preflight and resolved runtime paths.
3. Remove inherited aliases from environment files and automation.
4. Deploy only after the repository purity gate and production preflight pass.

## Verification

Regression tests must prove that:

- only the ClientPlatform namespace is resolved by `core/paths.py` and `core/runtime_paths.py`;
- production preflight requires ClientPlatform-owned runtime paths;
- tracked source contains no removed product identifier or inherited short-prefix namespace;
- database-driver guidance uses ClientPlatform examples;
- `DATABASE_URL` continues to work as the standard DSN contract.

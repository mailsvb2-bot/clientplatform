# ClientPlatform deterministic live-E2E staging fixture

## Purpose

Mutation-heavy live journeys need a repeatable synthetic tenant state. The
fixture is a CLI-only provisioning path; it is not an HTTP route and cannot be
called through the production application surface.

Use a new run key for every live-E2E run/attempt. Re-running the same key is
idempotent. `--reset` archives stale active businesses from older run keys in
the same fixture namespace and then seeds the current key. It never deletes
rows.

## Safety gates

The CLI refuses to mutate unless all of these are true:

- `APP_ENV=staging`;
- `CLIENTPLATFORM_LIVE_E2E=1`;
- `CLIENTPLATFORM_LIVE_E2E_TEST_ACCOUNTS=1`;
- `CLIENTPLATFORM_LIVE_E2E_PRODUCTION_CREDENTIALS=0`;
- `CLIENTPLATFORM_LIVE_E2E_REAL_MONEY=0`;
- `CLIENTPLATFORM_E2E_FIXTURE_DATABASE_ATTESTATION=synthetic-staging`.

It also requires a dedicated owner/member account, a bounded namespace/run key,
and synthetic customer subjects for Telegram, VK and MAX.

The reset path only archives businesses:

- owned by the dedicated fixture owner;
- whose names begin with the exact
  `[ClientPlatform E2E:<namespace>] ` prefix;
- that are not the current run-key's primary/secondary businesses.

There is no `DELETE`, `TRUNCATE` or production test endpoint.

## Seeded state

For each run key the fixture creates or reuses:

- primary and secondary businesses;
- owner workspace routing for Telegram/VK/MAX to the primary business;
- one manager member in the primary business only;
- UTC business profiles;
- consultations/program capabilities;
- primary and cross-tenant-denial offerings;
- a customer linked to the dedicated Telegram/VK/MAX synthetic identities;
- a published program with one text lesson;
- a stable far-future booking slot;
- a Telegram publication draft;
- an active offering price;
- a canonical manual payment fact using an idempotency key.

The manual payment is only an internal canonical ledger fact. It does not call a
payment provider and cannot spend real money.

## Invocation

First inspect the hermetic plan:

```bash
python scripts/clientplatform_live_e2e_fixture.py --plan
```

On the protected staging fixture host set:

```text
APP_ENV=staging
CLIENTPLATFORM_LIVE_E2E=1
CLIENTPLATFORM_LIVE_E2E_TEST_ACCOUNTS=1
CLIENTPLATFORM_LIVE_E2E_PRODUCTION_CREDENTIALS=0
CLIENTPLATFORM_LIVE_E2E_REAL_MONEY=0
CLIENTPLATFORM_E2E_FIXTURE_DATABASE_ATTESTATION=synthetic-staging
CLIENTPLATFORM_E2E_FIXTURE_NAMESPACE=canonical
CLIENTPLATFORM_E2E_FIXTURE_RUN_KEY=<unique run/attempt key>
CLIENTPLATFORM_E2E_FIXTURE_OWNER_USER_ID=<dedicated staging account id>
CLIENTPLATFORM_E2E_FIXTURE_MEMBER_USER_ID=<dedicated staging account id>
CLIENTPLATFORM_E2E_FIXTURE_CUSTOMER_TELEGRAM_SUBJECT=<dedicated subject>
CLIENTPLATFORM_E2E_FIXTURE_CUSTOMER_VK_SUBJECT=<dedicated subject>
CLIENTPLATFORM_E2E_FIXTURE_CUSTOMER_MAX_SUBJECT=<dedicated subject>
```

Then seed or rotate:

```bash
python scripts/clientplatform_live_e2e_fixture.py --seed --output artifacts/fixture.json
python scripts/clientplatform_live_e2e_fixture.py --reset --output artifacts/fixture.json
```

Only synthetic object IDs are written to the result. Provider credentials and
customer session material are never part of fixture output.

## Execution topology

Do not run fixture mutation independently on every phone/tablet matrix leg.
Provision it once from a protected staging-capable runner before a coordinated
live-E2E run, then give every device leg the same synthetic fixture IDs/run key.

This avoids one parallel device archiving another device's tenant and keeps the
cross-platform comparison on one canonical staging state.

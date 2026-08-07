# ADR-0077: Telegram Managed Bots as the primary personal-bot onboarding

**Status:** Accepted  
**Date:** 2026-08-07

## Context

The canonical ClientPlatform experience requires a non-technical business owner to create a personal client bot without copying tokens, configuring webhooks or understanding secret storage.

The existing fallback flow asks an owner to create a bot through BotFather and then provide the name of an operator-managed `CLIENTPLATFORM_SECRET_*` environment variable. That path is safe from raw-token storage but is not an acceptable primary mass-market onboarding.

Telegram Bot API 9.6 introduced Managed Bots. A manager bot can request creation of a child bot through a native keyboard request. Telegram then sends both a `managed_bot_created` service message and a `managed_bot` update. The manager can fetch the child bot token with `getManagedBotToken`.

## Decision

ClientPlatform will use Telegram Managed Bots as the primary personal-bot onboarding when the capability is explicitly enabled and production secret storage has passed preflight.

The user journey is:

```text
owner opens My Telegram bot
-> ClientPlatform checks manager-bot capability
-> owner presses Create my bot
-> Telegram opens native managed-bot creation
-> Telegram sends managed_bot_created
-> ClientPlatform fetches child token server-side
-> token is immediately age-encrypted
-> database stores only ciphertext plus vault:// reference
-> existing durable provisioning state machine verifies the child bot
-> polling gateway serves the new bot
-> owner sees Connected
```

The BotFather path remains available as a fallback for an already existing bot.

## Durable correlation

`ManagedBotCreated` contains the created bot but no ClientPlatform business identifier and no Bot API request identifier that can be trusted as a durable tenant key.

ClientPlatform therefore correlates the event through server-owned state:

- the initiating Telegram user must have an active BusinessMember membership;
- one Telegram user may have at most one outstanding `telegram_managed` provisioning request across their businesses;
- a repeated request in the same business returns the existing durable request;
- a second business is fail-closed until the first request is completed or cancelled;
- completion does not depend on FSM memory and survives a process restart;
- the service-message timestamp must not predate the active durable request, preventing a delayed event from an older cancelled attempt from binding to a newer request.

## Credential lifecycle

Raw child-bot tokens are never written to logs, FSM, callbacks, environment files, domain objects or connection rows.

The token lifecycle is:

```text
Telegram response in memory
-> AgeManagedBotCredentialVault.seal
-> managed_bot_credentials.ciphertext
-> vault://managed-bot/<business>/<credential> reference
-> connection.credential_reference
```

The existing runtime credential resolver accepts both reviewed `secret://env/...` fallback references and the new `vault://managed-bot/...` reference.

Token rotation updates the same business/bot credential record. Permanent local revocation marks the connection and managed bot revoked and overwrites the stored ciphertext with a non-secret tombstone in the same transaction.

## Production safety

Automatic onboarding is guarded by:

```text
CLIENTPLATFORM_MANAGED_BOT_AUTO_PROVISIONING_ENABLED=0
CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_IDENTITY_FILE=/run/secrets/clientplatform-managed-bot/identity.txt
CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_HOST_DIR=/var/lib/clientplatform/managed-bot-secrets
CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_ALLOW_GENERATE=0
```

The feature flag defaults to off.

When the flag is enabled, preflight requires:

- Managed Bot Gateway enabled;
- an absolute age identity path;
- no runtime key generation in deployed environments;
- a usable private age identity;
- polling-only Telegram transport.

The UI also checks `User.can_manage_bots` on the central control bot before creating a durable request.

## Migration

Existing SQLite and PostgreSQL databases have a provider CHECK constraint that previously accepted only `botfather`. Migration `clientplatform_managed_bot_provider_v1` extends this contract to:

```text
telegram_managed
botfather
```

Existing BotFather rows are preserved.

## Consequences

### Positive

- one-button creation replaces token-copy instructions for the normal owner journey;
- child-bot credentials are encrypted at rest and tenant-scoped;
- process restarts do not lose creation correlation;
- the existing polling gateway, lifecycle and provisioning machinery is reused rather than duplicated;
- the feature can be rolled out independently from advertising and Yandex Direct.

### Costs and limitations

- the central bot must be approved/configured by Telegram as able to manage bots;
- production requires a separately provisioned age identity;
- Telegram account bot-creation limits still apply to the owner;
- token/owner update events require lifecycle handling beyond the initial creation path;
- BotFather fallback remains during migration.

## Verification

Required regression evidence includes:

- native Telegram managed-bot button contract;
- `can_manage_bots` fail-closed behavior;
- restart-safe durable correlation;
- stale creation-event rejection;
- encrypted token storage with no raw-token persistence;
- cross-tenant rejection;
- credential rotation idempotency;
- permanent revoke erasing ciphertext;
- legacy SQLite schema migration;
- production preflight and feature-gate checks;
- existing BotFather fallback behavior;
- existing polling provisioning and PostgreSQL concurrency wall.

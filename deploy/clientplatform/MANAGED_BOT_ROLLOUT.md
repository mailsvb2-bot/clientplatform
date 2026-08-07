# ClientPlatform Telegram Managed Bots rollout

This runbook enables one-button personal-bot creation only after the central bot and encrypted credential storage are proven ready.

Do not use this runbook to deploy unrelated changes. Do not enable the feature before every preflight below passes.

## 1. Telegram capability

In the Telegram management UI for the central ClientPlatform bot, enable permission for the bot to manage bots created through it.

Before rollout, verify that `getMe` for the central bot exposes:

```text
can_manage_bots = true
```

If it is false or absent, leave automatic provisioning disabled. The BotFather fallback for an already existing bot remains available.

## 2. Provision the encryption identity

Create a dedicated directory on the host:

```bash
sudo install -d -m 0700 -o 10001 -g 10001 /var/lib/clientplatform/managed-bot-secrets
```

Generate a dedicated age X25519 identity outside the application process:

```bash
sudo -u '#10001' age-keygen \
  -o /var/lib/clientplatform/managed-bot-secrets/identity.txt
sudo chmod 0600 /var/lib/clientplatform/managed-bot-secrets/identity.txt
```

The private identity must never be committed to GitHub, copied into `clientplatform.env`, printed to logs or reused for Yandex advertising credentials.

The production Compose mount maps this directory read-only to:

```text
/run/secrets/clientplatform-managed-bot
```

## 3. Prepare environment

Keep these values exact:

```text
CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_IDENTITY_FILE=/run/secrets/clientplatform-managed-bot/identity.txt
CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_HOST_DIR=/var/lib/clientplatform/managed-bot-secrets
CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_ALLOW_GENERATE=0
CLIENTPLATFORM_BOT_GATEWAY_ENABLED=1
```

Keep automatic creation off for the first preflight:

```text
CLIENTPLATFORM_MANAGED_BOT_AUTO_PROVISIONING_ENABLED=0
```

Run the production environment preparation and normal ClientPlatform preflights.

## 4. Enable the guarded feature

Only after the identity exists, permissions are correct and the central bot reports `can_manage_bots=true`, set:

```text
CLIENTPLATFORM_MANAGED_BOT_AUTO_PROVISIONING_ENABLED=1
```

Then run:

```bash
python scripts/clientplatform_bot_gateway_preflight.py \
  --env-file /etc/clientplatform/clientplatform.env
```

Expected result:

```text
CLIENTPLATFORM_BOT_GATEWAY_PREFLIGHT_OK
```

The preflight proves the configured age identity can actually encrypt credential material. It does not print the identity or any token.

## 5. Staging smoke

Use a synthetic/staging business and a non-production owner account.

Expected journey:

```text
/mybot
-> Create my bot
-> native Telegram creation dialog
-> confirm bot name/username
-> Connected
```

Verify:

- the owner never sees or sends a token;
- `managed_bot_provisioning_requests.provider` is `telegram_managed`;
- the request becomes `completed`;
- `connections.credential_reference` starts with `vault://managed-bot/`;
- `managed_bot_credentials.ciphertext` does not contain the Telegram token;
- exactly one active managed bot route exists for the business;
- the child bot is served by polling and no webhook is registered;
- restarting the application does not lose the child route;
- a client can start the personal bot and reaches only the correct business.

Do not put token material into SQL evidence or shell history while checking these conditions.

## 6. Revocation smoke

From the owner UI, disable and reactivate the staging bot to prove the existing lifecycle still works.

Then use permanent revoke on a disposable bot and verify:

```text
connections.status = revoked
managed_bots.status = revoked
managed_bot_credentials.status = revoked
managed_bot_credentials.ciphertext = revoked
```

The permanent revoke path must fail closed if credential erasure cannot be committed.

## 7. Rollback

To stop new automatic creations without breaking already connected bots, set:

```text
CLIENTPLATFORM_MANAGED_BOT_AUTO_PROVISIONING_ENABLED=0
```

Restart the application through the normal release procedure.

This hides the native creation action but leaves existing active managed bots and the BotFather fallback intact.

Do not delete the age identity while any active `vault://managed-bot/...` connection exists; the polling gateway needs the identity to resolve those credentials.

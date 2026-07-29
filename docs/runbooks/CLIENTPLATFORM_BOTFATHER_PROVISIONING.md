# ClientPlatform BotFather managed bot provisioning

## Safety boundary

Telegram's ordinary Bot API does not create bot accounts. Create the bot in BotFather first. Never paste its token into a ClientPlatform chat, callback, form, issue, log, command history or database field.

The token and a separate webhook secret must be written directly to the reviewed production secret store. ClientPlatform receives only references shaped like:

```text
secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_<BOT>
secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_<BOT>
```

The webhook secret must be independent from the Telegram bot token, control-bot secret and every other managed bot.

## Operator sequence

1. Create the Telegram bot through BotFather and record the expected `@username`.
2. Generate an independent webhook secret.
3. Store both values under the `CLIENTPLATFORM_SECRET_*` namespace with restricted read permissions.
4. Create one provisioning request for the target business with a stable idempotency key and the expected username.
5. Submit only the two secret references.
6. Finalize provisioning once. ClientPlatform resolves the references, calls Telegram `getMe`, verifies the username, configures the tokenless gateway webhook and atomically creates the connection and managed-bot route.
7. Confirm the request is `completed`, the connection and bot are `active`, and no second active Telegram bot exists for the business.
8. Send a synthetic `/start` update and prove it opens only the target business customer portal.
9. Prove initial and follow-up program delivery use the new managed connection.

## Failure handling

- `awaiting_secret`: secret references have not been submitted.
- `ready`: safe to finalize.
- `verifying`: one verifier owns the lease. Do not start another attempt until the lease expires.
- `failed`: inspect the stable error code, repair the secret/provider issue, resubmit references and retry.
- `cancelled`: references were removed from the request; create or rearm a request deliberately.
- `completed`: finalization is idempotent and must not call Telegram again.

If a process dies during `verifying`, a new verifier may atomically recover the request after the bounded lease timeout. The previous lease token becomes invalid and cannot complete or fail the request.

If Telegram webhook setup succeeds but database finalization fails, ClientPlatform calls `deleteWebhook` as a compensating action and records `provisioning_commit_failed`. Verify the rollback before retrying.

## Go-live evidence

Before enabling traffic, prove:

- raw token material is absent from database rows, logs, webhook URLs and evidence artifacts;
- `getMe` returns the expected bot ID and username;
- the webhook URL is `https://<domain>/clientplatform/managed-bots/telegram/<bot-id>`;
- an invalid webhook secret is rejected indistinguishably from an unknown bot route;
- identical provisioning creation is idempotent under two PostgreSQL connections;
- only one verifier can acquire a fresh lease;
- only one verifier can recover a stale lease;
- the provisioning table is present in the PostgreSQL dump and disposable restore;
- cancellation clears the stored secret references;
- database conflict triggers webhook rollback and leaves no extra connection or managed-bot row.

## Rotation and replacement

Disable or revoke the old managed bot route before provisioning a replacement. The one-active-bot-per-business constraint must remain fail-closed. Rotate the token in the secret store without changing the database reference when the provider supports safe in-place rotation; otherwise revoke and provision a new route with a new idempotency key.

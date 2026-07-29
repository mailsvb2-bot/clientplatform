# ClientPlatform BotFather managed bot provisioning

## Safety boundary

Telegram's ordinary Bot API does not create bot accounts. Create the bot in BotFather first. Never paste its token into a ClientPlatform chat, callback, form, issue, log, command history or database field.

The token and a separate webhook secret must be written directly to the reviewed production secret store. ClientPlatform receives only references shaped like:

```text
secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_<BOT>
secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_<BOT>
```

The webhook secret must be independent from the Telegram bot token, control-bot secret and every other managed bot.

## Owner wizard

Open the target business dashboard and press **«Мой Telegram-бот»**. The same screen is available through `/mybot`; when the owner has several businesses, the bot asks which one to use.

The wizard has three steps:

1. Enter the expected BotFather `@username`. It must end with `bot`.
2. Enter only the environment-variable name that contains the BotFather token, for example `CLIENTPLATFORM_SECRET_TELEGRAM_MY_PRACTICE`.
3. Enter a different environment-variable name containing the independent webhook secret, for example `CLIENTPLATFORM_SECRET_WEBHOOK_MY_PRACTICE`.

The wizard accepts only reviewed `CLIENTPLATFORM_SECRET_*` names and converts them to `secret://env/...` references. It never asks for the secret values. If text resembles a raw Telegram token or another secret value, the bot attempts to delete that message immediately, does not echo the value and does not write it to the provisioning request.

After both references are saved, press **«Проверить и подключить»**. ClientPlatform resolves the references server-side, verifies the bot through Telegram `getMe`, compares the username, configures the tokenless webhook and commits the connection atomically.

The status screen supports refresh, correction of references, retry after a failed verification, safe cancellation before verification and return to the business dashboard. Callback payloads contain only compact UUID tokens and stay below Telegram's 64-byte limit.

## Owner lifecycle controls

A completed bot card contains **«Управление и состояние»**. This screen is tenant-scoped and never returns credential references, webhook-secret references, payload bodies or fleet-wide statistics. It shows only the public bot identity, local bot/connection status, safe queue counters and bounded timestamps for that business.

The available actions depend on the durable lifecycle state:

- `active`: **«Временно отключить»** and **«Отозвать навсегда»**;
- `disabled`: **«Включить снова»** and **«Отозвать навсегда»**;
- `revoked`: read-only status; the connection cannot be reactivated.

Temporary disable requires a confirmation click. ClientPlatform atomically disables both the managed-bot route and its connection, marks queued `pending`, `processing` and `retry` ingress events as `dead`, deletes their payloads and then asks Telegram to remove the webhook. If Telegram does not confirm removal, the local route remains closed and the owner sees a safe operator warning.

Reactivation first resolves the existing secret references, verifies `getMe`, checks the immutable bot ID and expected username and restores the tokenless webhook. Only after Telegram confirms the webhook does ClientPlatform atomically reactivate the local route. If another active bot now exists or the database transition fails, ClientPlatform removes the newly configured webhook as a compensating action and leaves the route disabled.

Permanent revoke uses a separate, explicit confirmation screen. ClientPlatform first commits the irreversible local revoke and payload cleanup, then requests webhook removal. A webhook-removal failure cannot reverse the local revoke; it produces a warning for operator follow-up. A revoked route can never be activated again and requires a new provisioning request.

## Operator sequence

1. Create the Telegram bot through BotFather and record the expected `@username`.
2. Generate an independent webhook secret.
3. Store both values under the `CLIENTPLATFORM_SECRET_*` namespace with restricted read permissions.
4. Ask the owner to open **«Мой Telegram-бот»**, or perform the wizard together with the owner.
5. Enter only the two secret-variable names; never enter their values.
6. Finalize provisioning once. ClientPlatform calls Telegram outside the database transaction and atomically creates the connection, managed-bot route and completed request.
7. Confirm the request is `completed`, the connection and bot are `active`, and no second active Telegram bot exists for the business.
8. Send a synthetic `/start` update and prove it opens only the target business customer portal.
9. Prove initial and follow-up program delivery use the new managed connection.

## Failure handling

- `awaiting_secret`: open the status screen and choose **«Указать ссылки на секреты»**.
- `ready`: choose **«Проверить и подключить»**.
- `verifying`: one verifier owns the lease. Refresh the status; do not start another attempt until the lease expires.
- `failed`: inspect the safe user-facing reason, repair the secret/provider issue and choose **«Повторить проверку»** or **«Исправить ссылки»**.
- `cancelled`: references were removed from the request; start a new connection deliberately.
- `completed`: finalization is idempotent and must not call Telegram again.

If a process dies during `verifying`, a new verifier may atomically recover the request after the bounded lease timeout. The previous lease token becomes invalid and cannot complete or fail the request.

If Telegram webhook setup succeeds but database finalization fails, ClientPlatform calls `deleteWebhook` as a compensating action and records `provisioning_commit_failed`. The wizard displays a safe retry message without exposing provider details or secret references.

For lifecycle operations, distinguish local state from provider synchronization:

- disable/revoke may complete locally while returning `webhook_detach_failed`; the route is already closed, but the operator must inspect Telegram webhook state;
- activation never reports success unless both Telegram webhook configuration and the local atomic transition succeed;
- a failed activation must leave the previous disabled route unavailable and must attempt webhook rollback;
- tenant or route lookup failures must not reveal whether another business owns the bot.

## Go-live evidence

Before enabling traffic, prove:

- the dashboard contains exactly one **«Мой Telegram-бот»** button and `/mybot` resolves the same tenant-scoped status;
- every wizard and lifecycle callback is at most 64 UTF-8 bytes;
- raw token material is absent from database rows, logs, status text, webhook URLs and evidence artifacts;
- an accidentally pasted token is deleted, not echoed and not persisted;
- `getMe` returns the expected bot ID and username;
- the webhook URL is `https://<domain>/clientplatform/managed-bots/telegram/<bot-id>`;
- an invalid webhook secret is rejected indistinguishably from an unknown bot route;
- identical provisioning creation is idempotent under two PostgreSQL connections;
- only one verifier can acquire a fresh lease;
- only one verifier can recover a stale lease;
- the provisioning table is present in the PostgreSQL dump and disposable restore;
- cancellation clears the stored secret references;
- database conflict triggers webhook rollback and leaves no extra connection or managed-bot row;
- owner health is tenant-scoped and contains no secret reference or payload;
- disable and revoke clear queued payloads and make route resolution fail closed;
- reactivation verifies the same Telegram identity before enabling the local route;
- a competing active bot prevents reactivation and triggers webhook compensation;
- revoke cannot be executed without its separate confirmation callback and cannot be reversed.

## Rotation and replacement

Disable or revoke the old managed bot route before provisioning a replacement. The one-active-bot-per-business constraint must remain fail-closed. Rotate the token in the secret store without changing the database reference when the provider supports safe in-place rotation; otherwise revoke and provision a new route with a new idempotency key.

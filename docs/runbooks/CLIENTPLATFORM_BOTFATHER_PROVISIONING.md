# ClientPlatform Telegram polling and BotFather provisioning

## Transport boundary

Telegram is polling-only in ClientPlatform:

- the central ClientPlatform bot uses long polling;
- every owner-managed BotFather bot uses long polling;
- no Telegram HTTP ingress route is registered;
- startup removes any stale Telegram webhook without dropping pending updates;
- `TELEGRAM_TRANSPORT=webhook` and `TELEGRAM_WEBHOOK_ENABLED=1` are rejected by production preflight and overridden to polling by the process entrypoint.

VK and MAX remain webhook-based and use the independent messenger HTTP ingress. Their `start`, `/start` and provider-native start events enter ClientPlatform directly; they must not redirect users to Telegram or show inherited product menus.

## Safety boundary

Telegram's ordinary Bot API does not create bot accounts. Create each managed bot in BotFather first. Never paste its token into a ClientPlatform chat, callback, form, issue, log, command history or database field.

Store the token directly in the reviewed production secret store. ClientPlatform receives only a reference shaped like:

```text
secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_<BOT>
```

A separate Telegram webhook secret is neither requested nor used.

## Central bot startup

The production process must start `main.py` with the dedicated central `BOT_TOKEN`. Before importing application settings, the entrypoint forces:

```text
TELEGRAM_TRANSPORT=polling
RUN_MODE=polling
TELEGRAM_WEBHOOK_ENABLED=0
TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED=0
```

It does not alter `MESSENGER_WEBHOOK_ENABLED`, `VK_WEBHOOK_ENABLED` or `MAX_WEBHOOK_ENABLED`.

At dispatcher startup ClientPlatform registers Telegram commands:

- `/start` — open ClientPlatform;
- `/mybot` — manage the owner's Telegram bot.

Telegram itself controls whether the large native **START** button is displayed. After the first interaction, the command-menu entry `/start` is the stable product-owned entry point. A temporary `setMyCommands` failure is logged but does not block long polling.

## Owner wizard

Open the target business dashboard and press **«Мой Telegram-бот»**. The same screen is available through `/mybot`; when the owner has several businesses, the bot asks which one to use.

The wizard has two steps:

1. Enter the expected BotFather `@username`. It must end with `bot`.
2. Enter only the environment-variable name that contains the BotFather token, for example `CLIENTPLATFORM_SECRET_TELEGRAM_MY_PRACTICE`.

The wizard accepts only reviewed `CLIENTPLATFORM_SECRET_*` names and converts them to `secret://env/...` references. It never asks for the secret value. If text resembles a raw Telegram token, the bot attempts to delete that message immediately, does not echo it and does not write it to the provisioning request.

An owner who was left on the historical third webhook-secret step is migrated safely: ClientPlatform ignores the newly typed value and finishes with the previously stored token reference.

After the token reference is saved, press **«Проверить и подключить»**. ClientPlatform resolves the token server-side, verifies the bot through Telegram `getMe`, compares the immutable bot ID and expected username, calls `deleteWebhook(drop_pending_updates=False)` and atomically commits the active polling route.

## Managed polling gateway

The managed-bot gateway periodically loads exactly the active Telegram managed-bot routes. For every active route it owns one long-polling task.

Each poller:

1. resolves the token only from the secret provider;
2. removes any stale webhook;
3. verifies the bot ID and username;
4. calls `getUpdates` with a bounded timeout;
5. admits each update into the existing durable, deduplicated ingress queue;
6. advances the Telegram offset only after durable admission.

Queue processing remains separate from polling. The existing worker establishes the customer/business link and feeds the update through the canonical dispatcher.

Disable or revoke can race with an outstanding `getUpdates` request. Admission therefore re-resolves the route inside the database write transaction. A stale poller cannot insert new events after the local route has closed.

Only one polling process may consume a given Telegram token. `TelegramConflictError` is reported as a polling conflict and retried with bounded backoff; it is never treated as a reason to switch to webhook.

## Owner lifecycle controls

A completed bot card contains **«Управление и состояние»**. This screen is tenant-scoped and never returns credential references, payload bodies or fleet-wide statistics. It shows the public bot identity, local bot/connection status, `polling` transport, safe queue counters and bounded timestamps for that business.

The available actions depend on durable lifecycle state:

- `active`: **«Временно отключить»** and **«Отозвать навсегда»**;
- `disabled`: **«Включить polling снова»** and **«Отозвать навсегда»**;
- `revoked`: read-only status; the connection cannot be reactivated.

Temporary disable requires confirmation. ClientPlatform atomically disables the route and connection, marks queued `pending`, `processing` and `retry` events as `dead`, clears payloads and asks Telegram to remove any stale webhook. The gateway reconciler then cancels that bot's poller.

Reactivation resolves the existing token reference, verifies `getMe`, checks immutable bot identity and removes any stale webhook. Only after that remote check does ClientPlatform atomically reactivate the route. The gateway reconciler starts its polling task. A competing active bot or database failure leaves the route disabled.

Permanent revoke uses a separate explicit confirmation screen. ClientPlatform commits the irreversible local revoke and payload cleanup, then attempts stale-webhook removal. The polling reconciler stops the task. A revoked route can never be activated again and requires a new provisioning request.

## VK and MAX entry

VK and MAX use webhook ingress. Their canonical entry aliases are:

```text
start
/start
старт
начать
главное меню
```

MAX provider-native events `bot_started`, `bot_start`, `chat_started` and `conversation_started` are also treated as entry even when no text is present.

Entry is registered through the canonical messenger identity map. The reply is persisted through the delivery outbox before the webhook is acknowledged. A repeated provider event is deduplicated before tenant creation or any other side effect.

A new VK/MAX user receives a direct ClientPlatform onboarding instruction and may create a workspace with:

```text
бизнес <название>
```

No Telegram handoff is required.

## Operator sequence

1. Configure the central `BOT_TOKEN` in the production secret environment.
2. Set Telegram transport to polling and both Telegram webhook flags to `0`.
3. Keep messenger HTTP ingress enabled only for the providers actually using webhooks, such as VK or MAX.
4. Create an owner bot through BotFather and record its expected `@username`.
5. Store its token under a restricted `CLIENTPLATFORM_SECRET_TELEGRAM_*` variable.
6. Enter only that variable name in **«Мой Telegram-бот»**.
7. Finalize provisioning once and confirm the request, connection and bot are `active`.
8. Confirm managed-gateway health reports `transport=polling` and one active poller for that bot.
9. Send `/start` to both the central and managed bot and prove the correct ClientPlatform workspace opens.
10. Send start events through enabled VK/MAX test endpoints and prove they open ClientPlatform without a Telegram redirect.

## Failure handling

- Central bot silent: verify the systemd/container process is running, uses the expected `BOT_TOKEN`, can reach `api.telegram.org`, and no second process consumes the same token.
- `TelegramConflictError`: locate and stop the duplicate polling process. Do not enable webhook.
- `awaiting_secret`: choose **«Указать ссылку на токен»**.
- `ready`: choose **«Проверить и подключить»**.
- `verifying`: one verifier owns the lease; refresh rather than starting another verifier.
- `failed`: repair username, secret-store reference, secret permissions or Telegram connectivity and retry.
- `cancelled`: start a new connection deliberately.
- `completed`: finalization is idempotent and must not repeat Telegram verification.
- Managed poller absent: inspect gateway health, active route state and secret-provider resolution.
- VK/MAX start silent: inspect provider webhook registration, webhook admission/dedupe table, delivery outbox and provider sender credentials.

## Go-live evidence

Before enabling traffic, prove:

- central Telegram runs only polling and has no registered webhook;
- managed Telegram bots run only polling and have no HTTP ingress route;
- `/start` and `/mybot` are present in the Telegram command menu;
- the central `/start` handler opens ClientPlatform;
- a managed `/start` opens only the linked business customer portal;
- every owner callback is at most 64 UTF-8 bytes;
- raw token material is absent from database payloads, logs, status text and evidence artifacts;
- an accidentally pasted token is deleted, not echoed and not persisted;
- polling removes stale webhook without dropping pending Telegram updates;
- duplicate polling ownership produces a visible conflict rather than a transport switch;
- durable admission remains idempotent under concurrent PostgreSQL connections;
- disable/revoke clear queued payloads and make route admission fail closed;
- VK `/start` returns canonical ClientPlatform content, not inherited product content;
- MAX `bot_started` works without a text message;
- repeated VK/MAX webhook events do not repeat tenant creation or replies;
- production preflight rejects every Telegram webhook configuration while permitting independent VK/MAX webhook ingress.

## Rotation and replacement

Rotate a token in the secret store without changing its database reference when the provider supports safe in-place rotation. The gateway detects the token digest change, closes the old bot session, verifies the new token and resumes polling. Otherwise disable or revoke the old route before provisioning a replacement. The one-active-bot-per-business constraint remains fail-closed.

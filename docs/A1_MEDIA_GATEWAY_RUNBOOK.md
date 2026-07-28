# A1 Media Gateway — deployment runbook

Этот runbook описывает отдельную A1-конфигурацию. Нельзя использовать production secrets, storage или Telegram bot Метротерапии.

## 1. Общая схема

```text
A1 delivery_dispatch_outbox
        |
        v
HmacMediaGatewayResolver
        |
        | short-lived HTTPS URL
        v
Telegram Bot API
        |
        | GET / HEAD / Range
        v
A1 Media Gateway
        |
        v
private filesystem или S3-compatible object storage
```

В outbox остаётся только исходный `s3://bucket/key`. Готовая HTTPS-ссылка и signing secret существуют только в памяти одного отправления.

## 2. Обязательные production-параметры

```bash
A1_MEDIA_GATEWAY_ENABLED=1
A1_MEDIA_GATEWAY_HOST=127.0.0.1
A1_MEDIA_GATEWAY_PORT=8091
A1_MEDIA_GATEWAY_BASE_URL=https://media.a1.example/a1
A1_MEDIA_GATEWAY_ALLOWED_BUCKETS=a1-private-media
A1_MEDIA_SIGNING_SECRET_REFERENCE=secret://env/A1_SECRET_MEDIA_SIGNING_KEY
A1_SECRET_MEDIA_SIGNING_KEY=<strong-random-secret>
```

`A1_MEDIA_GATEWAY_BASE_URL` должен указывать на reverse proxy/TLS endpoint, который без изменения path проксирует запросы к gateway process.

Пример:

```text
https://media.a1.example/a1/media/<bucket>/<key>?expires=...&sig=...
```

## 3. Filesystem mode

Подходит для локальной разработки и закрытого staging:

```bash
A1_MEDIA_GATEWAY_STORAGE_MODE=filesystem
A1_MEDIA_GATEWAY_FILESYSTEM_ROOT=/srv/a1-media
```

Объект `s3://a1-private-media/programs/lesson-01.mp3` читается из:

```text
/srv/a1-media/a1-private-media/programs/lesson-01.mp3
```

Root обязан быть абсолютным. Gateway проверяет, что итоговый resolved path не выходит из `<root>/<bucket>`.

## 4. S3-compatible mode

```bash
A1_MEDIA_GATEWAY_STORAGE_MODE=s3
A1_MEDIA_GATEWAY_S3_ENDPOINT=https://objects.example
A1_MEDIA_GATEWAY_S3_REGION=eu-west-1
A1_MEDIA_GATEWAY_S3_ACCESS_KEY_REFERENCE=secret://env/A1_SECRET_S3_ACCESS_KEY
A1_MEDIA_GATEWAY_S3_SECRET_KEY_REFERENCE=secret://env/A1_SECRET_S3_SECRET_KEY
A1_SECRET_S3_ACCESS_KEY=<access-key>
A1_SECRET_S3_SECRET_KEY=<secret-key>
```

Для временных credentials:

```bash
A1_MEDIA_GATEWAY_S3_SESSION_TOKEN_REFERENCE=secret://env/A1_SECRET_S3_SESSION_TOKEN
A1_SECRET_S3_SESSION_TOKEN=<session-token>
```

Текущий backend использует path-style HTTPS endpoint:

```text
https://objects.example/<bucket>/<key>
```

Storage policy должна разрешать только чтение нужных A1 bucket. Public ACL не требуется и не рекомендуется.

## 5. Bounds

```bash
A1_MEDIA_URL_TTL_SEC=300
A1_MEDIA_GATEWAY_MAX_OBJECT_BYTES=262144000
A1_MEDIA_GATEWAY_UPSTREAM_TIMEOUT_SEC=30
A1_MEDIA_GATEWAY_CHUNK_SIZE=65536
```

- URL TTL: 60–900 секунд;
- object size: от 1 MiB до 2 GiB;
- upstream timeout: 1–120 секунд;
- chunk: 4 KiB–1 MiB.

## 6. Reverse proxy

Reverse proxy должен:

- завершать TLS;
- сохранять original path и query string;
- не логировать query `sig` на общедоступном уровне;
- не кешировать ответы публично;
- пропускать `Range` и `Content-Range`;
- ограничивать методы `GET` и `HEAD`;
- иметь timeout больше `A1_MEDIA_GATEWAY_UPSTREAM_TIMEOUT_SEC`.

Gateway самостоятельно возвращает:

- `Cache-Control: private, no-store`;
- `Accept-Ranges: bytes`;
- `X-Content-Type-Options: nosniff`.

## 7. Самодостаточный реальный Telegram staging

Ручной workflow `A1 Telegram Staging` больше не требует заранее развёрнутого сервера, домена, S3 bucket, media object, signing key или GitHub Variables.

Он внутри одного GitHub Actions job:

1. создаёт небольшой валидный MP3 с проверяемым SHA-256;
2. генерирует одноразовый signing key и маскирует его;
3. запускает A1 Media Gateway только на `127.0.0.1`;
4. скачивает официальный `cloudflared` версии `2026.7.3`;
5. проверяет бинарник по закреплённому SHA-256;
6. создаёт временный `trycloudflare.com` HTTPS tunnel;
7. формирует подписанный URL и проверяет byte-range;
8. выполняет настоящий Telegram `sendAudio`;
9. останавливает tunnel и gateway;
10. сохраняет только безопасные диагностические логи.

Cloudflare Quick Tunnel используется исключительно как временная staging-граница. У него нет SLA, поэтому он запрещён для production и не является заменой постоянному gateway deployment.

### Единственный обязательный secret

Создать GitHub Environment с точным именем:

```text
a1-staging
```

Добавить Environment secret:

```text
A1_STAGING_TELEGRAM_BOT_TOKEN
```

Это должен быть токен отдельного тестового бота A1. Нельзя использовать бота Метротерапии или другого production-проекта.

### Подготовка Telegram

1. Создать отдельного бота через официальный `@BotFather`.
2. Открыть созданного бота со своего тестового Telegram-аккаунта.
3. Отправить ему `/start`.
4. Убедиться, что у staging-бота не настроен webhook.

Workflow вызовет `getWebhookInfo`, затем `getUpdates` и разрешит chat ID только когда найден ровно один приватный `/start`.

Опциональный Environment secret:

```text
A1_STAGING_TELEGRAM_CHAT_ID
```

Он нужен только если staging-боту отправили `/start` несколько разных аккаунтов. При явном chat ID автоматический `getUpdates` не выполняется.

### Запуск

```text
Actions -> A1 Telegram Staging -> Run workflow
```

Успешный прогон заканчивается сообщением:

```text
A1 Telegram staging smoke passed; message_id=<id>
```

Если `/start` не найден, workflow завершится с:

```text
staging_chat_not_discovered_send_start
```

Если у бота активен webhook:

```text
staging_telegram_bot_webhook_active
```

## 8. Проверка постоянного deployment

Операторский diagnostics payload постоянного gateway должен показывать:

```text
a1_media_gateway_configured=true
a1_media_gateway_health_available=true
a1_media_gateway_running=true
```

Дополнительно доступны агрегированные counters запросов, отказов, not-found, upstream errors и переданных байтов. Public health endpoint не раскрывает эти поля без diagnostics token.

## 9. Запрещено

- хранить raw bot token, S3 keys или signing key в repository;
- передавать bot token через `workflow_dispatch` input;
- помещать signed HTTPS URL в outbox;
- включать public-read bucket ACL ради Telegram;
- добавлять bucket без allowlist;
- использовать HTTP для public gateway или S3 endpoint;
- использовать Cloudflare Quick Tunnel как production endpoint;
- логировать Authorization header, token-bearing Telegram URL или signed query;
- направлять A1 на инфраструктуру исходной Метротерапии.

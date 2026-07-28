# ADR-0010: clientplatform media gateway и защищённый Telegram staging

## Статус

Принято.

## Контекст

clientplatform уже преобразует приватный `s3://bucket/key` в короткоживущую HMAC-ссылку и передаёт её Telegram. До этого этапа серверная сторона ссылки отсутствовала: подписанный URL не проверялся и приватный объект не стримился. Поэтому реальная media-доставка оставалась недоказанной end-to-end.

Прямая публикация bucket, public ACL или сохранение готового signed URL в outbox запрещены. Gateway должен быть optional, иметь одного владельца процесса, работать только с разрешёнными bucket и не раскрывать storage credentials, tenant identifiers, payload или signing key.

## Решение

Добавлен отдельный `clientplatform/runtime/media_gateway.py`.

Gateway:

1. выключен по умолчанию;
2. запускается только при `CLIENTPLATFORM_MEDIA_GATEWAY_ENABLED=1`;
3. принадлежит тому же process-wide `TaskManager`, что clientplatform dispatch;
4. проверяет точный raw path, expiry и HMAC через общий signing contract;
5. принимает только `expires` и `sig`, без дополнительных query-параметров;
6. повторно валидирует bucket/key и проверяет bucket allowlist;
7. поддерживает `GET`, `HEAD` и один byte range;
8. ограничивает размер объекта, timeout и streaming chunk;
9. отправляет `private, no-store`, `nosniff` и `Accept-Ranges`;
10. не возвращает клиенту детали storage/provider ошибок.

## Storage backends

### Filesystem

Предназначен для локальной разработки и hermetic integration tests. Root обязан быть абсолютным. После `resolve()` объект должен оставаться внутри `<root>/<bucket>`. Traversal, директории, отсутствующие и слишком большие объекты отклоняются.

### S3-compatible

Gateway выполняет приватный path-style `GET` по HTTPS и подписывает запрос AWS Signature Version 4.

- access key, secret key и optional session token разрешаются только через `CredentialProvider` непосредственно перед запросом;
- redirects запрещены;
- `Range`, если есть, включён в signed headers;
- raw secret key не попадает в URL, headers diagnostics, logs или ошибки;
- 404, 416, upstream/transport failures преобразуются в ограниченные gateway status codes.

## Конфигурация

Обязательные параметры при включении:

- `CLIENTPLATFORM_MEDIA_GATEWAY_BASE_URL` — публичный HTTPS base URL;
- `CLIENTPLATFORM_MEDIA_GATEWAY_ALLOWED_BUCKETS` — явный allowlist;
- `CLIENTPLATFORM_MEDIA_GATEWAY_STORAGE_MODE=filesystem|s3`;
- `CLIENTPLATFORM_MEDIA_SIGNING_SECRET_REFERENCE`;
- filesystem root либо S3 endpoint/region/credential references.

Основные bounds:

- `CLIENTPLATFORM_MEDIA_GATEWAY_MAX_OBJECT_BYTES`;
- `CLIENTPLATFORM_MEDIA_GATEWAY_UPSTREAM_TIMEOUT_SEC`;
- `CLIENTPLATFORM_MEDIA_GATEWAY_CHUNK_SIZE`;
- signed URL TTL остаётся 60–900 секунд.

## Telegram staging

Добавлен ручной workflow `clientplatform Telegram Staging`, защищённый GitHub Environment `clientplatform-staging`.

Workflow:

1. не запускается на push или pull request;
2. получает bot token и signing key только из environment secrets;
3. получает gateway base URL и подготовленный private media reference из environment variables;
4. делает `Range: bytes=0-0` probe подписанной gateway-ссылки;
5. только после успешного probe вызывает реальный Telegram `sendAudio`;
6. не печатает token, signed URL или signing key.

Отсутствующая конфигурация приводит к fail-closed завершению, а не к skip-success.

## Regression wall

Dependency-light clientplatform tests проверяют:

- deterministic HMAC и привязку к exact path/expiry;
- expired/future/tampered URL rejection;
- HTTPS, explicit storage и bucket allowlist config;
- filesystem traversal, size и Range;
- deterministic S3 SigV4, signed Range и отсутствие secret key в headers.

Полный CI дополнительно поднимает настоящий local aiohttp gateway и проверяет:

- full/HEAD/Range streaming;
- tampered/expired/extra-query rejection;
- `TelegramDispatchAdapter -> signed gateway URL -> fake Telegram HTTP provider -> gateway GET`;
- Telegram получает HTTPS URL, а не `s3://` и не signing secret.

## Риски и ограничения

- Реальный Telegram staging workflow требует отдельно настроенного GitHub Environment и доступного deployed HTTPS gateway; в обычном CI он намеренно не выполняется.
- Gateway пока не является upload API и не создаёт объекты в storage.
- Один signed URL может использоваться до expiry; одноразовый nonce не вводится, поскольку Telegram может повторять GET/Range.
- S3 implementation использует path-style endpoint. Virtual-hosted style можно добавить отдельным явным режимом.
- Production deployment и реальные secrets этим ADR не выполняются.

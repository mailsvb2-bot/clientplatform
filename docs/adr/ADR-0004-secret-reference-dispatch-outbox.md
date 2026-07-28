# ADR-0004: Secret references, transport-neutral dispatch и короткие транзакции

**Статус:** принято  
**Дата:** 2026-07-28

## Контекст

А1 должен обслуживать центрального Telegram-бота, персональные managed bots, Telegram Business, каналы, сообщества VK и ботов MAX для тысяч независимых бизнесов. Пользователь не работает с токенами вручную, но платформа обязана безопасно хранить и использовать их.

Логический факт «урок готов к выдаче» нельзя смешивать с конкретной попыткой отправки через Telegram/VK/MAX. Один урок в будущем может доставляться через разные разрешённые подключения, а временный сбой провайдера не должен ломать enrollment или создавать дубликаты.

## Решение

### Connection

`Connection` принадлежит одному `business_id` и описывает:

- платформу;
- тип подключения;
- внешний аккаунт/бот/сообщество;
- разрешения;
- состояние подключения;
- только `credential_reference`.

В БД не хранится сырой токен. Разрешены ссылки:

```text
secret://...
kms://...
vault://...
```

Ограничение проверяется доменом и `CHECK` в БД. Platform/type mapping также защищён доменом и БД.

### ManagedBot

`ManagedBot` — брендированный бот бизнеса, связанный с подходящим Connection. Webhook secret также хранится только как reference. Создание реального Telegram managed bot подключается отдельным API-адаптером позже; текущий слой фиксирует безопасную модель владения.

### Logical delivery и provider dispatch

- `LessonDelivery` означает, что конкретный урок должен быть выдан в рамках enrollment.
- `delivery_dispatch_outbox` означает конкретную попытку доставить этот logical delivery через определённые Connection и CustomerIdentity.

Dispatch содержит snapshot `payload_kind/payload_ref`, но не credential.

### Tenant/platform constraints

Составные внешние ключи требуют одновременно:

- logical delivery того же бизнеса;
- connection того же бизнеса и платформы;
- customer identity того же бизнеса и платформы.

Таким образом, ошибочный SQL не может отправить Telegram-сообщение через VK identity или использовать чужое подключение.

### Materialization

Один dispatch уникален для:

```text
business + logical_delivery + connection + customer_identity
```

Idempotency key также уникален внутри бизнеса. Повторная команда возвращает существующий dispatch.

### Lease и claim

Worker арендует bounded batch:

- PostgreSQL: `FOR UPDATE SKIP LOCKED`;
- SQLite/test: условный выбор и lock token;
- stale `sending` lease возвращается в обработку после TTL;
- settlement требует точный `lock_token`;
- worker с потерянной арендой не может записать успех или ошибку.

### Сеть вне транзакции

Алгоритм worker:

1. короткая транзакция claim;
2. commit и закрытие DB context;
3. разрешение credential reference;
4. сетевой вызов adapter;
5. новая короткая транзакция mark sent/retry/dead.

Медленный Telegram/VK/MAX API не держит блокировку PostgreSQL.

### Retry policy

- временная ошибка: `retry`, exponential backoff, Connection остаётся `active`;
- успешная отправка: dispatch `sent`, logical delivery `sent`, progress `delivered`, connection health очищается;
- исчерпание попыток: dispatch `dead`, logical delivery `failed`, connection `attention`;
- cancellation worker освобождает аренду в `retry` без потери работы.

### Credential boundary

Worker получает из outbox только reference. `CredentialProvider` разрешает reference непосредственно перед отправкой. Adapter получает raw secret только в памяти одного вызова.

Запрещено:

- записывать resolved secret в БД;
- включать его в payload;
- логировать его;
- возвращать frontend;
- сохранять в exception text.

Worker редактирует ошибку перед сохранением, заменяя известный resolved secret на `[redacted]`.

### Adapter boundary

Transport-neutral worker выбирает adapter по платформе. Telegram adapter использует внедрённый `TelegramBotClient` и маршрутизирует:

- audio;
- video;
- document;
- image;
- text/link/task/mixed.

Реальный HTTP client не входит в доменную модель и подключается следующим слоем.

## Рассмотренные варианты

### Хранить токен в `connections.token`

Отклонено. Утечка БД, backup, log или admin query раскрывает все боты.

### Один outbox сразу с токеном и chat ID

Отклонено. Секрет размножается в очереди и artifacts, а ротация становится ненадёжной.

### Делать сетевой send внутри DB transaction

Отклонено. Медленная сеть удерживает lock, увеличивает contention и усложняет recovery.

### Ставить Connection в attention после первой ошибки

Отклонено. Claim выбирает только active connections, поэтому transient retry заблокировал бы сам себя. `attention` ставится только после terminal failure.

## Privacy

- `connections` и `managed_bots` — retain как бизнес-интеграции и audit references;
- `delivery_dispatch_outbox` — erase, поскольку содержит recipient routing и payload snapshot;
- startup требует полную зарегистрированную clientplatform-схему;
- модульные schema-тесты могут проверять только созданный слой, но неизвестная таблица с `business_id` всегда fail-closed.

## Проверки

- raw token отклоняется доменом и БД;
- type/platform mismatch отклоняется;
- только owner/administrator управляют connections;
- duplicate connection/materialization идемпотентны;
- identity должна принадлежать customer enrollment и платформе connection;
- cross-business dispatch невидим;
- lease эксклюзивен и stale lease восстанавливается;
- transient retry остаётся claimable;
- terminal failure уходит в dead/attention;
- wrong lock token не меняет состояние;
- disabled connection не claimable;
- Telegram adapter получает resolved secret, но outbox его не содержит;
- worker редактирует secret из persisted error;
- cancellation возвращает lease в retry;
- privacy manifest покрывает весь connection/outbox слой.

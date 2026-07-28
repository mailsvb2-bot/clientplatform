# ADR-0008: A1 dispatch в health/readiness приложения

## Статус

Принято.

## Контекст

ADR-0007 подключил optional A1 dispatch runtime к каноническому `TaskManager`. Однако общий health server продолжал видеть только унаследованный scheduler. Если `A1_DISPATCH_RUNTIME_ENABLED=1`, но A1 owner не создал scheduler, scheduler остановился, текущий tick завершился ошибкой или давно не было успешного tick, `/readyz` оставался зелёным.

Это создавало ложную готовность: приложение принимало бы трафик и новые задания, хотя `delivery_dispatch_outbox` фактически не обслуживается.

При этом A1 runtime выключен по умолчанию и не должен ухудшать readiness унаследованного приложения, пока владелец явно его не включил.

## Решение

Общий `runtime/health_server.py` получает additive A1 snapshot через публичный lifecycle API.

Health diagnostics показывают:

- включён ли A1 dispatch конфигурацией;
- доступна ли runtime-диагностика;
- создан ли lifecycle owner;
- запущен ли scheduler;
- число итераций, claimed/sent/retried/dead dispatch;
- число ошибок, текущую ошибку и возраст последнего успешного tick.

Readiness действует так:

1. при выключенном `A1_DISPATCH_RUNTIME_ENABLED` A1 нейтрален и не влияет на legacy readiness;
2. при включённом runtime диагностика должна быть доступна;
3. lifecycle должен быть composed;
4. scheduler должен быть enabled и running;
5. текущая tick-ошибка делает readiness красным до следующего успешного tick;
6. после хотя бы одного успешного tick его возраст не должен превышать `A1_DISPATCH_READY_MAX_LAST_TICK_AGE_SEC`;
7. default stale threshold — 180 секунд, чтобы не конфликтовать с bounded tick timeout;
8. подробности остаются защищены существующим operator diagnostics token, публичный probe по-прежнему сообщает только `ok`, `service` и `probe`.

## Ошибки readiness

- `a1_dispatch:health_unavailable`;
- `a1_dispatch:not_composed`;
- `a1_dispatch:not_enabled`;
- `a1_dispatch:not_running`;
- `a1_dispatch:recent_tick_error`;
- `a1_dispatch:stale_tick`.

## Почему individual dead-letter не делает readiness красным

Один terminal dispatch может означать отозванный пользовательский токен, удалённый чат или некорректный материал конкретного бизнеса. Это бизнес-ошибка с изоляцией отказа, а не обязательно отказ платформенного worker. Счётчик `dead` остаётся в health diagnostics, но readiness оценивает способность scheduler продолжать работу.

Отдельный backlog/dead-letter SLO должен опираться на состояние outbox в базе и будет добавлен вместе с A1 operational metrics.

## Риски и ограничения

- Snapshot читается внутри общего process health endpoint и не доказывает доступность внешнего Telegram API.
- До первого завершённого tick stale-проверка не применяется; зависший tick всё равно ограничен scheduler timeout и затем становится `recent_tick_error`.
- Health endpoint сохраняет legacy service name для совместимости существующих probes.
- Это эксплуатационная готовность dispatch runtime, а не готовность всего A1 MVP.

## Проверки

- выключенный A1 не влияет на readiness;
- включённый runtime fail-closed при недоступной диагностике;
- отсутствующий owner блокирует readiness;
- работающий scheduler считается готовым;
- текущая tick-ошибка блокирует readiness;
- устаревший успешный tick блокирует readiness;
- health содержит A1 diagnostics;
- общий readiness возвращает HTTP 500 при остановившемся включённом A1 scheduler;
- A1 Boundary Diagnostics запускает новый regression wall.

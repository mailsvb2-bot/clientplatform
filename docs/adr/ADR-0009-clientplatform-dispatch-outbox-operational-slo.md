# ADR-0009: DB-backed operational SLO для clientplatform dispatch outbox

## Статус

Принято.

## Контекст

ADR-0008 сделал lifecycle и scheduler clientplatform dispatch наблюдаемыми. Однако работающий scheduler ещё не доказывает, что очередь обслуживается с приемлемой скоростью. Возможны состояния:

- worker продолжает выполнять ticks, но due backlog растёт быстрее обработки;
- одна старая запись остаётся due длительное время при небольшом общем backlog;
- lease остаются в `sending` дольше lock TTL;
- terminal failures резко возрастают, хотя scheduler формально жив;
- чтение outbox недоступно, а process health продолжает выглядеть исправным.

Readiness без данных из source-of-truth базы в этих случаях является ложнозелёным.

## Решение

Добавляется один агрегирующий DB-запрос к `delivery_dispatch_outbox`. Он не читает tenant identifiers, external subjects, credentials, payload references или тексты ошибок и возвращает только глобальные эксплуатационные счётчики:

- pending;
- retry;
- sending;
- sent;
- dead;
- cancelled;
- due сейчас;
- stale sending leases;
- dead за ограниченное временное окно;
- возраст самой старой due-записи.

PostgreSQL и SQLite используют один совместимый агрегат через существующий DB compatibility layer.

Outbox probe выполняется только когда `CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED=1`. Выключенный clientplatform не делает лишних запросов и не влияет на legacy readiness.

## Readiness thresholds

- `CLIENTPLATFORM_DISPATCH_READY_MAX_DUE`, default `1000`;
- `CLIENTPLATFORM_DISPATCH_READY_MAX_OLDEST_DUE_AGE_SEC`, default `900`;
- `CLIENTPLATFORM_DISPATCH_READY_MAX_STALE_SENDING`, default `0`;
- `CLIENTPLATFORM_DISPATCH_READY_MAX_RECENT_DEAD`, default `100`;
- `CLIENTPLATFORM_DISPATCH_READY_DEAD_WINDOW_SEC`, default `900`.

Stale lease рассчитывается относительно канонического `CLIENTPLATFORM_DISPATCH_LOCK_TTL_SEC`.

## Ошибки readiness

- `clientplatform_dispatch_outbox:unavailable`;
- `clientplatform_dispatch_outbox:due_backlog`;
- `clientplatform_dispatch_outbox:oldest_due`;
- `clientplatform_dispatch_outbox:stale_sending`;
- `clientplatform_dispatch_outbox:recent_dead`.

## Почему используется recent dead, а не весь dead-letter

Dead-letter является терминальным историческим фактом. Суммарный счётчик монотонно растёт и без процедуры acknowledgement со временем сделал бы readiness постоянно красным из-за старых уже разобранных случаев.

Поэтому health показывает полный dead count, а readiness ограничивает только новый burst за настраиваемое окно. Это обнаруживает системную деградацию транспорта, не смешивая её с историческим аудитом.

## Почему учитываются и backlog, и возраст

Только лимит количества пропускает одну навсегда зависшую запись. Только лимит возраста пропускает резкий массовый backlog, который ещё не успел состариться. Два независимых условия обнаруживают обе формы деградации.

## Безопасность

- агрегат не выбирает `business_id`, `customer_identity_id`, `payload_ref`, `last_error` или secret references;
- DB error сохраняется только как имя класса исключения;
- подробные поля остаются за существующим diagnostics token;
- публичный `/healthz` и `/readyz` по-прежнему возвращает только минимальный probe payload.

## Ограничения

- thresholds являются deployment policy и должны калиброваться по staging/load evidence;
- глобальный агрегат предназначен для readiness платформы, а tenant-level dashboards потребуют отдельной авторизованной аналитики;
- readiness не заменяет alerting, dashboards и on-call процедуру разбора dead-letter;
- Telegram staging smoke остаётся отдельным внешним доказательством.

## Проверки

- выключенный clientplatform не обращается к outbox;
- aggregate query не читает tenant/payload данные;
- SQLite/PostgreSQL-compatible placeholders и timestamp boundaries;
- DB failure редактируется до типа ошибки и fail-closed;
- due backlog и oldest due независимо блокируют readiness;
- stale leases и recent dead burst блокируют readiness;
- здоровая пустая очередь сохраняет readiness;
- clientplatform Boundary Diagnostics запускает outbox observability regression wall.

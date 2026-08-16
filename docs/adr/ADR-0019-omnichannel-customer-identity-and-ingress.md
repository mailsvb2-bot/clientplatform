# ADR-0019: Telegram, VK и MAX используют одного Customer и разные provider adapters

**Статус:** принято  
**Дата:** 2026-08-16

## Контекст

ClientPlatform уже определяет `Customer` как tenant-scoped бизнес-сущность и `CustomerIdentity` как внешнюю идентичность внутри конкретного `Business` (ADR-0002). В кодовой базе также исторически присутствуют VK/MAX sender/webhook/account-link механизмы, унаследованные из исходной Метротерапии.

Эти механизмы полезны как проверенные provider adapters, но их legacy `account_id`, меню и продуктовый state не могут становиться вторым источником истины ClientPlatform. Для массовой платформы один и тот же клиент специалиста должен видеть один прогресс, записи, продажи, программы и коммуникационное состояние независимо от того, пришёл он через Telegram, VK или MAX.

Отдельные глобальные VK/MAX webhooks также недостаточны для мультитенантной платформы: provider payload не является авторизацией `business_id`, а один глобальный mutable account mapping нарушил бы tenant boundary.

## Решение

1. Единственная клиентская бизнес-истина остаётся `business_id + customer_id`.
2. Telegram, VK и MAX представлены отдельными `CustomerIdentity`, которые могут ссылаться на один и тот же `customer_id` внутри одного бизнеса.
3. Автоматического merge разных уже занятых identity нет. Явное связывание выполняется только одноразовым `cplink_...` grant.
4. Raw link token возвращается только в момент выдачи. Durable storage содержит только SHA-256 digest, business/customer scope, optional target platform, expiry и consume evidence.
5. Consume атомарен и single-use; повтор, истечение, другой business, другая target platform и identity, уже принадлежащая другому Customer, завершаются fail-closed.
6. VK/MAX ingress route привязывается к существующей активной `Connection` и хранит только secret reference (`secret://`, `kms://`, `vault://`).
7. В URL webhook допустим только opaque route UUID. `business_id`, `connection_id`, credential и expected external provider account разрешаются сервером.
8. На каждом admission route повторно проверяется против активных `Business` и `Connection`; tenant ID из provider/client payload не принимается на доверии.
9. Provider replay/dedupe key включает canonical route id, поэтому одинаковые provider event IDs разных бизнесов не конфликтуют.
10. Входящий человеческий текст Telegram/VK/MAX идёт в один Sales/Sales AI application contour и тот же consent boundary. Provider-specific меню Метротерапии не является ClientPlatform intent model.
11. Исходящие сообщения Telegram/VK/MAX используют один `delivery_dispatch_outbox`, leases, retries, dead handling, credential resolution и media resolver. Различается только transport adapter.
12. Существующие hardened VK/MAX provider senders могут использоваться ниже transport boundary как implementation detail. Они не владеют Customer, Sales, Program, booking, consent или durable delivery truth.
13. Если retained provider client не доказывает native capability (например native video), adapter использует честный поддерживаемый file/document fallback вместо выдуманной parity.
14. Legacy global VK/MAX endpoints сохраняются только как временный compatibility surface; новый ClientPlatform contour включается отдельным feature flag и не зависит от legacy account/menu state.
15. Production rollout выполняется отдельно от code merge и только после secret/connection/route provisioning, migrations, health/readiness и synthetic cross-channel probes.

## Почему не копируем Метротерапию целиком

У Метротерапии уже есть полезные provider HTTP/upload/retry и linking-механизмы, но её предметная модель рассчитана на один продукт и legacy account. Механическое копирование создало бы второй customer/account graph, второй UI state и риск расхождения Telegram/VK/MAX.

Выбрано переносить только provider-level mechanics и проверенные failure semantics, а identity, tenant authorization, delivery, Sales и business state оставлять каноническими ClientPlatform.

## Идемпотентность и конкуренция

- first-seen external identity защищена уникальным `(business_id, platform, external_subject)`;
- link consume сериализуется conditional update `consumed_at IS NULL`;
- occupied identity никогда не переносится молча;
- inbound provider event защищён durable route-scoped dedupe;
- outbound VK `random_id` детерминирован canonical dispatch idempotency key;
- общий dispatch outbox не создаёт новый логический delivery при worker retry/restart.

## Приватность

Новые durable таблицы содержат `business_id` и обязаны быть явно классифицированы privacy manifest. Link-token table хранит provider subject только как consume evidence после фактической привязки; raw secret/link token не хранится. Erasure Customer должен каскадно удалять/анонимизировать допустимые customer-linked grants согласно privacy policy; route/connection operational evidence живёт на business scope.

## Rollout

1. применить additive schema;
2. создать/проверить business-scoped VK/MAX `Connection`;
3. создать ingress route с runtime-only webhook secret reference;
4. настроить provider callback на `/clientplatform/webhooks/<platform>/<route_id>`;
5. включить `CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED=1` только после конфигурационного preflight;
6. выполнить synthetic VK и MAX ingress, cross-channel link, outbound text/media и duplicate replay probes;
7. проверить Sales/customer timeline, health/readiness и отсутствие cross-tenant доступа;
8. legacy global endpoints отключать только после доказанного перехода всех нужных routes.

Rollback: выключение feature flag мгновенно убирает новые canonical VK/MAX HTTP routes; additive data остаётся сохранённой, существующий Telegram и legacy compatibility contour не переписываются.

## Проверки

- first-seen Telegram/VK/MAX создаёт/разрешает tenant-scoped identity;
- одинаковый external subject в разных business остаётся независимым;
- one-time link связывает второй/третий channel с тем же `customer_id`;
- wrong-business, wrong-platform, expired, repeated и conflicting link fail-closed;
- route невозможно зарегистрировать на чужую/неактивную Connection или другой external account;
- webhook secret обязателен и сравнивается constant-time;
- provider duplicate не создаёт второй Sales signal;
- VK/MAX используют тот же canonical dispatch worker, что Telegram;
- retry/restart не создаёт новый logical delivery;
- privacy/tenant/schema/static/PostgreSQL gates остаются зелёными.

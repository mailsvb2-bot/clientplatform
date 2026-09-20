# ADR-0125: revision, freshness and retraction lifecycle for external observations

**Статус:** принято реализацией vertical slice

## Контекст

ADR-0124 добавил в ClientPlatform provider-neutral external observations поверх
существующего signed External Product Bridge. Первый slice специально был read-only и
не давал observation права запускать business action. Для выпускаемого UIII consumer
этого недостаточно: источник должен уметь исправить ошибку, отозвать факт, восстановить
его новой проверкой и явно передать известную либо неизвестную свежесть.

ClientPlatform остаётся владельцем Customer, CustomerIdentity, Sales, Outcome,
AutomationPolicy, consent/suppression и Customer Timeline. Новый lifecycle не создаёт
второй CRM или decision engine.

## Решение

1. Customer-bound structured observation получает стабильный `observation_key`,
   положительную точную `revision`, `state=active|retracted`, optional
   `supersedes_external_event_id` и optional `fresh_until`.
2. Revision 1 всегда active и ничего не supersede. Revision N>1 обязана ссылаться на
   exact current head revision N-1. Gap, stale branch и перенос цепочки на другого
   Customer fail closed.
3. Ingress сериализует observation transitions на tenant connector row; database
   дополнительно имеет unique revision/supersedes indexes. Это не позволяет двум
   конкурентным событиям создать две головы одной цепочки.
4. Receipt остаётся immutable evidence. Исправление не перезаписывает старый receipt:
   новая revision добавляется рядом, а Customer Timeline проецирует только current head.
5. `retracted` head показывается как отзыв, а не как действующий факт. Следующая active
   revision может восстановить цепочку с новым provenance.
6. `fresh_until` никогда не выдумывается. Если source не передал срок, UI показывает
   «свежесть неизвестна». Если срок истёк — «возможно устарело после …».
7. Connector disable остаётся tenant off-switch: disabled connector не принимает новые
   observations, но ClientPlatform продолжает работать и сохраняет уже принятые receipts.
8. Служебная `CustomerPlatform.INTERNAL` identity используется только для binding и
   больше не показывается владельцу как контакт или fallback display name.
9. Ни active, ни stale, ни retracted observation сами не создают Outcome, sales stage,
   follow-up, approval, send или AutomationPolicy authority.

## Хранилище

Источник истины остаётся `external_product_event_receipts`. Additive lifecycle columns:

- `observation_key`;
- `observation_revision`;
- `observation_state`;
- `observation_supersedes_event_id`;
- `observation_fresh_until`.

Отдельный EvidenceStore/CRM не создаётся. Migration
`clientplatform_external_observation_lifecycle_v1` добавляет колонки и partial indexes
для существующих SQLite/PostgreSQL установок.

## Пользовательский результат

В Customer Timeline владелец видит только текущую версию внешнего observation:

- источник и качество evidence;
- когда факт реально наблюдался;
- номер ревизии;
- актуально ли наблюдение, возможно ли оно устарело или свежесть неизвестна;
- известные ограничения;
- явный статус «Внешнее наблюдение отозвано», если источник отменил факт.

Старое ошибочное observation больше не выглядит текущей истиной, но audit evidence
остаётся в immutable receipts.

## Не входит в этот slice

- влияние external observations на Sales/Next Best Action;
- автоматические действия по observation;
- live credentials или доказательство конкретного UIII/provider account;
- generic Person graph;
- owner UI для ручного split/merge identity;
- отдельный UIII runtime.

Следующий consumer slice может учитывать active/non-stale observation как input в
существующем policy/decision contour только после live useful-source proof и без обхода
AutomationPolicy/consent.

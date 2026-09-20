# ADR-0124: provenance-bearing external observations for Customer context

**Статус:** принято реализацией vertical slice

## Контекст

ClientPlatform уже владеет каноническими `Customer`, `CustomerIdentity`, sales/outcome,
AutomationPolicy, consent/suppression и Customer Timeline. Внешняя инфраструктура наподобие
UIII может приносить новое разрешённое наблюдение (например, подтверждённое участие во
внешнем вебинаре/LMS или результат звонка), но не должна становиться второй CRM, вторым
identity authority или скрытым разрешением на коммуникацию.

Существующий External Product Bridge уже даёт tenant-scoped HMAC ingress, durable receipt,
idempotency и bounded metadata. Создание отдельного UIII event store внутри ClientPlatform
дублировало бы канонические контуры.

## Решение

1. Первый consumer slice расширяет существующий `ExternalProductEventType.EVIDENCE`
   структурированным `ExternalProductObservation`.
2. Observation хранит отдельные поля: stable kind, понятный label, `observed_at`,
   `provenance_ref`, evidence quality и bounded limitations. Общего числового
   `confidence` нет: происхождение/качество не выдаются за вероятность истинности.
3. Подпись connector-а подтверждает границу источника, но не превращает содержимое
   observation в безусловно истинный факт и не разрешает бизнес-действие.
4. Публичный webhook по-прежнему не принимает `business_id` или `customer_id`.
   Связать connector-scoped `customer_ref` с уже существующим Customer можно только
   серверным explicit binding через действующий tenant/RBAC boundary.
5. Raw `customer_ref` не сохраняется. Binding использует тот же one-way
   connector-scoped fingerprint/internal identity, что и ingress. Существующий binding
   никогда не переносится на другого Customer автоматически: конфликт fail-closed.
6. Structured observation сохраняется в существующем
   `external_product_event_receipts.metadata_json`; новая таблица истины не создаётся.
7. Customer Timeline read-only проецирует только observations с известной schema version
   и показывает owner-у источник, время наблюдения, качество и ограничения.
8. Observation не создаёт `BusinessOutcomeEvent`, approval, follow-up, рассылку,
   изменение CRM-стадии или AutomationPolicy. Последующее действие допускается только
   отдельным каноническим решением ClientPlatform.
9. Anonymous evidence остаётся допустимым и не создаёт fake Customer. Неизвестная связь
   остаётся неизвестной.
10. UIII не является runtime-зависимостью ClientPlatform: тот же consumer contract может
    использовать любой разрешённый внешний adapter. Отказ внешнего источника не
    останавливает основные функции ClientPlatform.

## Первый пользовательский результат

Owner открывает существующую карточку Customer и видит дополнительное внешнее наблюдение
в той же chronology, например «Участие в вебинаре подтверждено», вместе с источником,
временем наблюдения, качеством evidence и известными ограничениями. История Customer,
sales, payments и outcomes остаётся канонической историей ClientPlatform.

## Риски и ограничения

- Этот slice не доказывает live-доступ конкретного UIII/provider account.
- Explicit binding предназначен для доверенного server-side consumer adapter; owner UI
  для ручного связывания может быть добавлен отдельным vertical slice.
- Retraction/revision внешнего observation пока не добавлены; до этого observation
  используется как исторический read-only контекст и не может само запускать действия.
- Provider-supplied provenance может быть корректно подписан и при этом фактически
  ошибаться; интерфейс поэтому показывает качество/ограничения, а не «истину».

## Проверки

- structured observation schema и reserved metadata;
- evidence-only invariant;
- explicit binding к существующему Customer;
- cross-tenant и conflicting binding fail-closed;
- raw external customer ref не хранится;
- observation не создаёт outcome;
- Customer Timeline/Cockpit показывают source/observed time/quality/limitations;
- существующий generic external-product ingress остаётся backward-compatible.

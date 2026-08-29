# ADR-0123: signed external-product fact ingress into canonical customer/outcome/attribution state

**Статус:** предложено патчем

## Контекст

ClientPlatform должен уметь управлять коммерческим контуром продукта, который живёт в другом runtime и другом репозитории, не импортируя его код и не создавая вторую CRM/аналитику. Внешний продукт может подтверждать факты пользовательского пути и оплаты, но не должен сам выбирать tenant, customer id или финансовую атрибуцию ClientPlatform.

## Решение

1. Внешний продукт регистрируется в tenant-scoped `external_product_connectors`. В таблице хранится только `webhook_secret_reference` из namespace `secret://env/CLIENTPLATFORM_SECRET_*`.
2. Публичный ingress включается отдельно флагом `CLIENTPLATFORM_EXTERNAL_PRODUCT_INGRESS_ENABLED=1` и имеет маршрут `POST /clientplatform/external-products/{connector_id}/events`.
3. Tenant определяется только по connector id из маршрута. Поля `business_id`, `customer_id`, `outcome_event_id` и другие внутренние identifiers входному payload не разрешены.
4. Каждый запрос подписывается HMAC-SHA256 по `<unix timestamp>.<raw body>`. Допустимое окно повторной передачи — 5 минут; body ограничен 64 KiB.
5. Контракт v1 принимает только канонические виды фактов: `evidence`, `lead_created`, `lead_qualified`, `order_paid`, `refund_recorded`. Domain-specific события переводятся в них адаптером конкретного продукта.
6. `customer_ref` никогда не сохраняется как есть. ClientPlatform создаёт deterministic internal identity по SHA-256 fingerprint, scoped connector id + business.
7. Acquisition metadata может сформировать first-touch через существующий `AttributionRepository`. Новый источник не создаёт второй attribution engine.
8. Бизнес-результаты пишутся в `business_outcome_events`. Подтверждённые деньги сразу проходят через существующий `RevenueAttributionRepository`; refund обязан ссылаться на принятый `order_paid` event.
9. `(business_id, connector_id, external_event_id)` является durable idempotency boundary. Повтор идентичного body возвращает тот же receipt; повтор event id с другим fingerprint блокируется как конфликт.
10. `external_product_event_receipts` хранит доказательство приёма, fingerprints и bounded metadata, но не сырой external customer id.

## Следствия

External product остаётся самостоятельным runtime, а ClientPlatform получает только проверенные бизнес-факты. Customer timeline, Money Cockpit и revenue attribution продолжают строиться из единственного канонического состояния ClientPlatform.

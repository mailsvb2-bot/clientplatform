# ADR-0122: owner-approved email outbound through the canonical provider outbox

**Статус:** предложено патчем

## Контекст

ClientPlatform уже умеет находить и оценивать партнёров, готовить персональный `PartnerContentPack` и отправлять разрешённый Telegram first contact через общий `provider_dispatch_outbox`. Публичный деловой email полезен для B2B-партнёрств, но сам факт публикации адреса не является согласием на автоматическую рассылку.

## Решение

1. Email добавляется как `ConnectionPlatform.EMAIL` только для generic provider work. Клиентская выдача уроков и customer identity routing остаются на Telegram/VK/MAX и не расширяются этим ADR.
2. Первая реализация connection type — `email_smtp`. SMTP credential хранится только за `vault://connection/...` с purpose `smtp_credentials`; `connections` содержит только reference.
3. Connection создаётся `pending` и становится `active` только после успешного SMTP authentication probe.
4. `existing_relationship` и `opted_in` сохраняют существующее право first contact. `public_business_contact` требует отдельного явного owner approval конкретного candidate, connection, recipient и payload.
5. В `partner_outreach_approvals` не сохраняются email и текст письма: только SHA-256 fingerprints, tenant/member linkage и lifecycle approval.
6. Email dispatch использует тот же lease/idempotency/credential worker. Второй scheduler, второй outbox worker и отдельная CRM не создаются.
7. Перед provider I/O live authorization проверяется повторно. После durable SMTP non-replay boundary неоднозначный результат не переотправляется автоматически и переводится в manual reconciliation.
8. Deterministic `Message-ID` строится из idempotency key как дополнительная provider-side защита; он не заменяет durable outbox idempotency.
9. Reply ingestion по email этим патчем не вводится. Ответы могут быть добавлены отдельным inbound connector без изменения outbound-инвариантов.

## Следствия

Публичный B2B email становится исполнимым каналом после осознанного действия владельца, а ClientPlatform сохраняет прежний запрет на бесконтрольную холодную рассылку. Telegram/VK/MAX runtime и customer delivery semantics не меняются.

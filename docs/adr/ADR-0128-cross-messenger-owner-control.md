# ADR-0128 — Cross-messenger owner control

**Статус:** accepted  
**Дата:** 2026-08-30

## Контекст

ClientPlatform уже имеет канонический tenant-scoped owner/member UI для VK и MAX, а также отдельный глобальный bootstrap, который умеет зарегистрировать identity и создать `Business` без Telegram. До этого изменения глобальный VK/MAX entry после создания бизнеса оставался отдельной минимальной поверхностью: он перечислял рабочие пространства, но не продолжал пользователя в тот же owner control-plane.

Это создавало ложный продуктовый паритет: backend позволял создать бизнес, однако полный управляющий путь оставался Telegram-centric. Одновременно `cpa_*` является публичным acquisition-маршрутом клиента конкретного бизнеса и не может использоваться как owner entry ClientPlatform.

## Решение

1. Официальные точки входа ClientPlatform в Telegram, VK и MAX считаются управляющим контуром владельца/сотрудника.
2. Собственные Telegram-боты, сообщества VK и MAX-боты бизнеса остаются отдельными tenant-scoped клиентскими каналами.
3. Глобальный VK/MAX owner entry после серверного разрешения единственного доступного `Business` переиспользует канонический `native_member_interactions` renderer.
4. Канонический renderer получает публичный channel-neutral adapter, который не требует tenant webhook route и не материализует provider outbox самостоятельно.
5. Создание `Business` во VK/MAX сразу продолжает onboarding шагом описания деятельности. Этот шаг сохраняется через существующий application layer, а не в отдельной предметной модели.
6. Для пользователей с несколькими `Business` автоматический выбор запрещён; отдельный безопасный selector будет следующим вертикальным срезом.
7. Защищённые setup-ссылки и provider buttons остаются отдельным следующим срезом. Секреты, route credentials и tenant selection не переносятся в глобальный payload.

## Безопасность

- `business_id` не принимается из произвольного пользовательского текста для автоматического tenant resolution.
- Owner control разрешается только через `list_accessible_businesses` + `resolve_tenant_context`.
- При нескольких бизнесах система не угадывает активный tenant.
- Tenant-scoped VK/MAX ingress и его durable dispatch/outbox остаются без изменений.
- `cpa_*` не становится owner-командой и сохраняет customer acquisition семантику.

## Последствия

Первый срез даёт новому владельцу VK/MAX непрерывный путь: `start -> Business -> деятельность -> канонический owner menu`, а существующему владельцу с одним бизнесом — прямой вход в тот же native owner surface. Кнопочный transport parity, безопасный выбор одного из нескольких бизнесов и выдача short-lived `/clientplatform/connect/...` из официального control entry продолжаются следующими срезами.

## Проверки

- channel-neutral parser различает owner control и onboarding activity;
- VK `/start` и MAX `bot_started` сохраняют bootstrap без Telegram;
- создание бизнеса предлагает следующий onboarding step;
- single-business owner entry вызывает канонический native renderer;
- activity step сохраняет профиль и возвращает owner dashboard;
- webhook dedupe сохраняется до side effects.

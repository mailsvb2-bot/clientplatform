# ADR-0128 — Cross-messenger owner control

**Статус:** accepted  
**Дата:** 2026-08-30

## Контекст

ClientPlatform уже имеет канонический tenant-scoped owner/member UI для VK и MAX, а также отдельный глобальный bootstrap, который умеет зарегистрировать identity и создать `Business` без Telegram. До этого изменения глобальный VK/MAX entry после создания бизнеса оставался отдельной минимальной поверхностью: он перечислял рабочие пространства, но не продолжал пользователя в тот же owner control-plane.

Это создавало ложный продуктовый паритет: backend позволял создать бизнес, однако полный управляющий путь оставался Telegram-centric. Одновременно `cpa_*` является публичным acquisition-маршрутом клиента конкретного бизнеса и не может использоваться как owner entry ClientPlatform.

## Решение

1. Официальные точки входа ClientPlatform в Telegram, VK и MAX считаются управляющим контуром владельца/сотрудника.
2. Собственные Telegram-боты, сообщества VK и MAX-боты бизнеса остаются отдельными tenant-scoped клиентскими каналами.
3. Глобальный VK/MAX owner entry после серверного разрешения доступного `Business` переиспользует канонический `native_member_interactions` renderer.
4. Канонический renderer получает публичный channel-neutral adapter, который не требует tenant webhook route и не материализует provider outbox самостоятельно.
5. Создание `Business` во VK/MAX сразу продолжает onboarding шагом описания деятельности. Этот шаг сохраняется через существующий application layer, а не в отдельной предметной модели.
6. Для пользователей с несколькими `Business` используется server-authorized selector: messenger получает только короткую ссылку на UUID, а каждый выбор повторно проверяется через текущий список доступных бизнесов. После явного выбора `(user_id, platform) -> business_id` сохраняется в каноническом tenancy-хранилище только как routing context для последующих текстовых шагов; этот контекст не является авторизацией и при каждом использовании повторно валидируется активным membership.
7. Глобальный owner entry сохраняет канонический `CustomerInteractionMessage` и доставляет его нативными inline-кнопками VK/MAX. `cpm:setup:*` и `cpm:switch:*` хранятся как несекретные команды; short-lived HTTPS URL материализуется только непосредственно перед provider I/O. Секреты и route credentials не переносятся в глобальный payload.
8. Мутации получают idempotency key, привязанный к provider event и payload, а официальный MAX owner entry best-effort подтверждает `message_callback` до бизнес-обработки.

## Безопасность

- `business_id` не принимается из произвольного пользовательского текста для автоматического tenant resolution.
- Owner control разрешается только через `list_accessible_businesses` + `resolve_tenant_context`.
- При нескольких бизнесах система не угадывает tenant: активный workspace появляется только после явного server-validated выбора и повторно авторизуется перед каждым использованием.
- Tenant-scoped VK/MAX ingress и его durable dispatch/outbox остаются без изменений.
- `cpa_*` не становится owner-командой и сохраняет customer acquisition семантику.

## Последствия

Этот срез даёт новому владельцу VK/MAX непрерывный путь: `start -> Business -> деятельность -> канонический owner menu`, владельцу с одним бизнесом — прямой вход, а владельцу нескольких бизнесов — безопасный selector с сохранением выбранного workspace для многошаговых текстовых продолжений. Канонические действия отображаются реальными VK/MAX-кнопками, а защищённые `/clientplatform/connect/...` materialize только на границе доставки.

## Проверки

- channel-neutral parser различает owner control и onboarding activity;
- VK `/start` и MAX `bot_started` сохраняют bootstrap без Telegram;
- создание бизнеса предлагает следующий onboarding step;
- single-business owner entry вызывает канонический native renderer;
- activity step сохраняет профиль и возвращает owner dashboard;
- VK/MAX получают provider-native callback buttons из одного interaction payload;
- setup bearer URL отсутствует в durable reply payload и материализуется только перед отправкой;
- неразрешимая setup-ссылка fail-closed и не превращается во внутреннюю callback-команду;
- multi-business selector повторно проверяет membership, а выбранный workspace переживает новый inbound event и не даёт чужого tenant access;
- owner mutation idempotency различает независимые provider events и сохраняет retry одного события;
- официальный MAX `message_callback` получает best-effort acknowledgement;
- webhook dedupe сохраняется до side effects.

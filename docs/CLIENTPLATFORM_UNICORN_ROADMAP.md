# ClientPlatform Unicorn Roadmap

**Статус:** обязательный исполнительный roadmap проекта.  
**Подчинён:** `docs/CLIENTPLATFORM_CANON_TZ.md` — Канон остаётся единственным нормативным документом продукта.  
**Назначение:** превратить Канон в последовательность проверяемых вертикальных доработок, которые следующие чаты/агенты могут продолжать без потери контекста.  
**Базовая точка:** ClientPlatform `v16.1`, `main` = `de0c332e0f4ed3bea408b7da4319cda04da58a69` на момент создания roadmap.  
**Правило:** фактическое состояние всегда перепроверяется по текущему `main`; этот SHA — исторический anchor, а не вечный target.

---

## 1. Зачем существует этот roadmap

ClientPlatform строится не как CRM, рекламный кабинет, конструктор ботов или набор AI-интеграций. Цель — **цифровой сотрудник малого бизнеса**, который принимает понятное намерение владельца, безопасно выполняет техническую работу и возвращает измеримый бизнес-результат.

Целевая петля продукта:

```text
Намерение владельца
        ↓
План ClientPlatform
        ↓
Безопасное выполнение
        ↓
Измеримый результат
        ↓
Объяснение результата
        ↓
Следующее лучшее действие
        ↓
Повтор / автопилот в утверждённых границах
```

Эталонный пользовательский сценарий:

```text
Подключился
→ рассказал, чем занимается
→ подключил канал/рекламный кабинет/оплату
→ нажал «🚀 Найти новых клиентов»
→ ClientPlatform подготовил и выполнил разрешённые действия
→ пришёл лид
→ лид записался
→ клиент оплатил
→ владелец увидел, сколько вложено и сколько заработано
→ ClientPlatform предложил или автоматически выполнил следующий шаг в утверждённых пределах
```

Пользователь не обязан понимать `CampaignId`, OAuth, webhook, очередь, UTM, attribution model, bidding strategy, retry, idempotency key, cron или устройство LLM.

---

# 2. Иерархия источников истины

| Источник | За что отвечает |
|---|---|
| `docs/CLIENTPLATFORM_CANON_TZ.md` | кем является продукт, его неизменные принципы и архитектурные запреты |
| этот roadmap | что делать дальше, в каком порядке и по каким критериям считать работу законченной |
| `AGENTS.md` | обязательный стартовый и исполнительный протокол следующего чата/агента |
| `docs/adr/*` | принятые архитектурные решения по отдельным спорным изменениям |
| текущий `main` + БД + CI | факты о реально реализованном состоянии |
| README | понятное объяснение проекта человеку; не нормативный источник |

При конфликте roadmap с Каноном приоритет у Канона. При конфликте roadmap-status с кодом приоритет у фактов текущего `main`, а roadmap должен быть исправлен.

---

# 3. North Star: что означает «делаем единорога»

«Единорог» здесь не означает максимальное количество функций. Он означает сочетание пяти вещей:

1. **Очевидная ценность:** обычный предприниматель быстро получает результат без технической команды.
2. **Измеримость:** платформа умеет доказать связь своих действий с лидами, записями, оплатами, повторными продажами и экономией времени.
3. **Повторяемость:** один backend и единые доменные механизмы масштабируются на тысячи бизнесов без ручного обслуживания каждого.
4. **Автопилот:** всё больше рутины выполняется автоматически, но только внутри прозрачных, обратимых и ограниченных владельцем политик.
5. **Экономический moat:** со временем ClientPlatform лучше понимает конкретный бизнес, его рабочие каналы, офферы и циклы продаж, не нарушая tenant isolation и приватность.

## 3.1. North-star metric

Главная продуктовая метрика:

**MBMO — Monthly Businesses With Measurable Outcome**  
Количество активных бизнесов, которые за месяц получили через ClientPlatform хотя бы один достоверно измеримый результат:

- квалифицированный лид;
- подтверждённую запись;
- оплаченный заказ/услугу;
- повторную продажу;
- возвращённого клиента;
- другой канонически определённый денежный/операционный outcome.

Метрика должна считаться из durable outcome ledger, а не из событий интерфейса или LLM-оценок.

## 3.2. Обязательные поддерживающие метрики

- Time to First Value / Time to First Measurable Outcome.
- Activation rate: бизнес завершил минимальную настройку и запустил первый результативный сценарий.
- Lead → booking conversion.
- Booking → payment conversion.
- CPL / cost per booking / CAC — только там, где attribution достаточно достоверна.
- Revenue / attributed revenue / contribution margin — с явной валютой и моделью attribution.
- D30 / D90 business retention.
- Доля бизнесов, использующих AutomationPolicy/автопилот.
- Доля предложенных ClientPlatform действий, принятых владельцем.
- Paid conversion и expansion по тарифам/usage.
- COGS инфраструктуры, сообщений, AI и внешних провайдеров как доля выручки.
- Support burden на один активный бизнес.
- Критические safety metrics: tenant leak = 0, unauthorized spend = 0, secret exposure = 0.

Нельзя оптимизировать продукт только под клики, количество отправленных сообщений или количество созданных AI-объектов.

---

# 4. Product flywheel

```text
Привлечение
  ↓
Лид
  ↓
Продажа / запись
  ↓
Доставка услуги / программы / материала
  ↓
Удержание и повторная продажа
  ↓
Измерение результата
  ↓
Понимание, что работает
  ↓
Следующее лучшее действие / автопилот
  ↓
Рост бизнеса и ценности ClientPlatform
  ↓
Реферал / партнёр / расширение тарифа
  ↺
```

ClientPlatform должен закрывать эту петлю единым tenant-scoped набором сущностей и событий, а не независимыми «мини-продуктами» с собственными источниками истины.

---

# 5. Что уже считается DONE на базовой точке v16.1

Ниже — не обещания, а baseline, подтверждённый текущим кодом/README/CI на момент создания roadmap.

### `DONE` — мультитенантное ядро

- `Business`, `BusinessMember`, tenant context и RBAC.
- Несколько бизнесов у одного пользователя.
- Отдельные owner/customer boundaries.
- Tenant-aware repository/application contracts.
- Privacy/erasure primitives.

### `DONE` — Telegram operational surface

- Центральный управляющий бот ClientPlatform.
- Owner onboarding/admin UX.
- Managed client bots и gateway/provisioning contracts.
- Services, customers, booking/schedule, materials/programs и background jobs.

### `DONE` — goal-first acquisition foundation

- Каноническое owner-действие `🚀 Найти новых клиентов`.
- Единый navigation contract для acquisition entry/recovery.
- Goal-first / one-click подготовка рекламы без требования от владельца понимать внутренние campaign IDs.

### `DONE` — Yandex Direct managed lifecycle

Evidence: PR #160, merged to baseline `main` SHA `de0c332e0f4ed3bea408b7da4319cda04da58a69`.

- tenant-scoped Yandex OAuth connection;
- durable `ManagedAdCampaign` binding;
- mapping `business_id + promotion_campaign_id + connection_id → external_campaign_id`;
- opaque deterministic ClientPlatform ownership marker;
- exact reconciliation вместо «угадывания» внешней кампании;
- creation через Yandex Direct `/v501/` как `UNIFIED_CAMPAIGN`;
- Search и Network создаются с `SERVING_OFF`;
- повторная проверка ID/name/type/strategy перед managed publication;
- fail-closed при ambiguity/uncertain provisioning;
- idempotent managed ad-group/ad publication path;
- старые публикации сохраняют совместимость через legacy publication path.

### `DONE` — ad-spend safety foundation

- explicit consent boundary;
- hard cap / daily cap;
- immutable consent receipt;
- stale-state / duplicate-tap protection;
- concurrency protection;
- kill switch / stop controls;
- paid AI media usage отделено от бесплатной подготовки.

### `DONE` — production engineering foundation

- PostgreSQL production topology;
- Caddy HTTPS surface;
- fail-closed preflight;
- safe updater/rollback/stability window;
- encrypted backup и optional off-site replication;
- production isolation;
- user scenario / concurrency / security / release gates.

### `DONE` — quality ratchets

На baseline roadmap:

```text
combined coverage: 74.05%
branch coverage:   65.11%
```

Порог повышается при улучшении и не снижается ради зелёного CI.

---

# 6. Исполнительный статус и порядок

Статусы:

- `DONE` — merged в `main` с evidence.
- `NEXT` — единственный default slice для следующего чата, если владелец не дал другую задачу.
- `QUEUED` — выполнить последовательно после `NEXT`.
- `PLANNED` — важная работа после immediate queue.
- `BLOCKED-LIVE` — код готов, но требуется отдельная реальная provider/production проверка.
- `DEFERRED` — сознательно отложено с причиной.

**По умолчанию в один момент существует только один `NEXT`.** Это защищает проект от параллельных полуреализаций.

---

# 7. Immediate execution queue — ближайшие вертикальные slices

## U-001 — `DONE` — Durable Outcome Ledger

Evidence: PR #170, merge SHA `85f676db72e57e9281b04dd623291d087c1b4d56`; все PR workflows завершились `success`, включая Canon, CI quality/coverage, static security, PostgreSQL payment/concurrency, booking concurrency, production isolation и pre-deploy gate.

### Цель

Создать единый append-only источник истины о бизнес-результатах, чтобы ClientPlatform мог измерять не «что нажали», а что реально произошло с бизнесом.

### Добавить/расширить

Предпочтительные целевые файлы:

```text
clientplatform/domain/outcomes.py
clientplatform/application/outcomes.py
clientplatform/infrastructure/outcome_repository.py
services/db/schema/clientplatform_outcomes.py
tests/test_clientplatform_outcomes.py
tests/test_clientplatform_outcomes_tenant_isolation.py
```

Перед созданием каждого файла проверить current `main`; если эквивалентный canonical module уже существует — расширить его, а не создавать дубль.

### Домен

Минимальные сущности:

```text
BusinessOutcomeEvent
OutcomeType
OutcomeSource
OutcomeMoney
```

Минимальные outcome-типы первой версии:

```text
lead_created
lead_qualified
booking_created
booking_confirmed
booking_completed
order_paid
customer_reactivated
refund_recorded
```

Поля события минимум:

```text
id
business_id
outcome_type
occurred_at
source_type
source_id
customer_id? / subject_ref?
amount_minor?
currency?
idempotency_key
metadata_version
created_at
```

### Инварианты

- append-only business fact; correction оформляется новым событием, а не тихим переписыванием истории;
- idempotency key обязательно business-scoped;
- деньги только integer minor units + ISO currency;
- monetary outcome без валюты запрещён;
- никакой cross-tenant subject resolution;
- provider/UI retry не должен создавать двойной outcome;
- LLM не может самостоятельно объявить денежный outcome подтверждённым фактом.

### Первый end-to-end hook

Подключить ledger минимум к одному уже существующему реальному пути, предпочтительно `booking_created` + имеющемуся каноническому payment/order событию, если оно существует в current `main`. Не создавать параллельный order/payment domain только ради этого slice.

### Tests / gates

- happy path;
- duplicate/idempotent write;
- invalid/mixed monetary payload;
- cross-tenant read/write rejection;
- correction/reversal semantics;
- existing regression contour green;
- coverage baseline не снижать.

### DONE когда

Outcome хранится durable, читается только своим бизнесом, реальный существующий vertical пишет его end-to-end, и PR merged в `main` с evidence.

### Метрика, которую открывает

MBMO и все последующие business-result analytics.

---

## U-002 — `DONE` — Acquisition Attribution Spine

Evidence: PR #172, merge SHA `c7f85182e3d100c82de87c996b34ab5e71fbf31b`; все 15 PR workflows завершились `success`, включая Canon, CI quality/coverage, static security, booking/ad-spend/partner concurrency, production isolation, user scenario matrix и pre-deploy gate.

### Цель

Пронести источник привлечения от первого touch до Customer/lead/booking/order так, чтобы реклама и продажи можно было связать без догадок.

### Целевые модули

```text
clientplatform/domain/attribution.py
clientplatform/application/attribution.py
clientplatform/infrastructure/attribution_repository.py
services/db/schema/clientplatform_attribution.py
tests/test_clientplatform_attribution.py
tests/test_clientplatform_attribution_tenant_isolation.py
```

Расширить существующие promotion/customer/booking/order primitives, не клонировать их.

### Сущности

```text
AcquisitionTouch
AttributionIdentity
AttributionLink
AttributionModelVersion
```

Touch должен уметь ссылаться на известные источники:

```text
organic
referral
telegram
vk
max
website
yandex_direct
partner
manual/import
unknown
```

Provider-specific refs могут включать `promotion_campaign_id`, managed external campaign, ad/adgroup, tracking token, UTM и deep-link identity, но только как данные — не как источник бизнес-правил.

### Инварианты

- tracking token opaque и business-scoped;
- tenant-id из client URL/deep-link никогда не принимается на доверии;
- повторный tap не размножает first-party identity;
- исходный touch не переписывается задним числом без audit/version;
- expired/forged token fail-closed;
- персональные данные минимизируются.

### DONE когда

Можно взять один acquisition touch и доказуемо провести его минимум до booking/customer в рамках одного бизнеса.

---

## U-003 — `DONE` — Revenue Attribution & Unit Economics

Evidence: PR #174, merge SHA `148bc3ba1732cbe530973d20284308cc16b6df70`; все 15 PR workflows завершились `success`, включая Canon, CI quality/coverage, static security, booking/ad-spend/partner concurrency, production isolation, user scenario matrix и pre-deploy gate.

### Цель

Связать outcome ledger, acquisition и деньги, чтобы владелец видел бизнес-результат, а не только рекламные метрики.

### Добавить/расширить

```text
clientplatform/domain/revenue_attribution.py
clientplatform/application/revenue_attribution.py
clientplatform/infrastructure/revenue_attribution_repository.py
services/db/schema/clientplatform_revenue_attribution.py
tests/test_clientplatform_revenue_attribution.py
```

Если current `main` уже содержит подходящий canonical analytics/attribution module — расширить его.

### Первая объяснимая модель

Начать с детерминированной versioned модели, например `last_non_direct` или иной явно выбранной ADR-моделью. Не отдавать распределение денег LLM.

### Выходные показатели

```text
spend
leads
qualified_leads
bookings
paid_customers
attributed_revenue
CPL
cost_per_booking
CAC
ROAS/ROMI only when valid
```

### Жёсткие правила

- неизвестная/смешанная валюта → не суммировать деньги;
- неполная attribution → UI обязан сказать об этом;
- никакого выдуманного revenue;
- refund/reversal уменьшает attributable economics отдельным outcome;
- модель attribution и версия сохраняются с результатом.

### DONE когда

На тестовом business можно получить воспроизводимый путь `touch → booking/order → revenue attribution` и объяснить каждое число.

---

## U-004 — `DONE` — Yandex Campaign-level Read Analytics

Evidence: PR #177, merge SHA `c5ad2187402593152688cc90a60f559830f148f6`; все 15 PR workflows завершились `success`, включая Canon, CI quality/coverage, static security, booking/ad-spend/partner concurrency, production isolation, user scenario matrix и pre-deploy gate; coverage baseline повышен до 74.16% combined / 65.22% branch.

### Цель

Дополнить точный AdId-контур отдельным read-only CampaignId reporting path, чтобы видеть performance managed/других совместимых кампаний даже там, где AdId недоступен.

### Основные файлы

```text
clientplatform/integrations/yandex_direct_analytics.py
clientplatform/application/ad_connections.py
handlers/clientplatform_yandex_analytics.py
```

При необходимости добавить только отдельные provider result models, не второй analytics engine.

### Требования

- фильтр по конкретному CampaignId;
- поля минимум `CampaignId`, `CampaignName`, `Impressions`, `Clicks`, `Cost`;
- existing exact-AdId attribution оставить неизменным;
- campaign diagnostics явно маркировать как diagnostics, если их нельзя честно приписать конкретным ClientPlatform outcomes;
- Yandex async report `201/202` корректно обрабатывать;
- numeric parsing fail-closed;
- auth refresh/revoke semantics не ослаблять.

### Tests

- строки без AdId;
- pending report;
- malformed money/numeric;
- auth refresh;
- campaign mismatch;
- mixed/unknown currency handling.

### DONE когда

Owner UI способен показать честный read-only campaign performance отдельно от attributable business result.

---

## U-005 — `DONE` — Managed Yandex Activation Policy

Evidence: PR #194, merge SHA `d5a518167515df1cab5086f24aaf2eddcff1f1ff`; все 15 PR workflows завершились `success`, включая Canon, CI quality/coverage, static security, ad-spend/booking/partner concurrency, production isolation, user scenario matrix и pre-deploy gate.

### Цель

Довести текущую безопасно созданную `SERVING_OFF` managed campaign до управляемого запуска расходов — без обхода существующей consent architecture.

### Основные зоны

```text
clientplatform/integrations/yandex_direct.py
clientplatform/integrations/yandex_direct_budget.py
clientplatform/integrations/yandex_direct_actions.py
clientplatform/application/ad_connections.py
handlers/clientplatform_goal_launch.py
handlers/clientplatform_goal_autopilot.py
```

Точные названия current modules перепроверить перед изменением.

### Правильный контракт

```text
managed campaign exists SERVING_OFF
→ draft prepared
→ owner sees concrete spend boundary
→ immutable consent/policy persisted
→ provider state re-fetched
→ strategy/budget mutation within exact approved limits
→ launch
→ continuous stop controls
```

### Запрещено

- автоматически придумывать бюджет;
- выводить кампанию из `SERVING_OFF` только потому, что в аккаунте есть деньги;
- повторно использовать старое согласие после изменения cap/currency/meaningful targeting;
- отключать kill switch/concurrency/idempotency;
- зависеть от ручного CampaignId пользователя.

### DONE когда

Тесты доказывают, что ни один путь не включает spend без актуального разрешения владельца или явной AutomationPolicy.

---

## U-006 — `DONE` — Growth Cockpit: «что сегодня происходит с бизнесом»

### Цель

Объединить технические данные в один результативный owner-view.

### Поверхности

- быстрый Telegram summary;
- полный `dashboard/` / Telegram Mini App surface.

### Главные блоки

```text
Сегодня
Новые лиды
Кому нужно ответить
Записи
Оплаты
Реклама
Что сработало
Что требует решения
Что ClientPlatform сделает дальше
```

### Требования

- business context определяется сервером;
- периоды минимум 7/30 дней;
- любая цифра имеет source/meaning;
- actionable alert ведёт прямо в следующий шаг;
- provider jargon по умолчанию скрыт;
- при неполной attribution честно показывается ограничение.

### DONE когда

Владелец за один экран понимает: что получил, сколько это стоило, где деньги/лиды и что делать дальше.

### Evidence

- PR #196 (`U-006: add canonical Growth Cockpit`) merged в `main` как `65472747f9b0ed6f2941b21309a16f4f6c426c5d`.
- Все 15 обязательных PR workflows на точном head `dc83b988b33b933d3a248d2b16ab0cc71ebe8217` завершились `success`, включая Canon, CI, pre-deploy, production isolation, scenario matrix и concurrency contours.
- Coverage-ratchets сохранены без снижения baseline: combined `74.21% / 74.21%`, branch `65.28% / 65.28%`; full coverage run: `3616 passed, 7 skipped`.
- Acceptance закреплён canonical Growth Cockpit domain/dashboard/handler tests, dependency-light Canon, owner navigation contracts и существующими sales/attribution command routes без второго business brain.

---

## U-007 — `DONE` — Zero-to-First-Outcome Onboarding

### Цель

Новый бизнес должен перейти от регистрации к первому полезному результату без ручной технической настройки.

### Расширить BusinessProfile

Проверить текущую модель и дополнить canonical fields при необходимости:

```text
business name
description
offers/services
prices
audiences
geo
working hours
contacts
booking rules
tone of voice
allowed/prohibited claims
legal/compliance constraints
brand assets
source URLs/content
preferred conversion action
```

### UX

1. коротко рассказать о бизнесе;
2. ClientPlatform строит структурированный паспорт;
3. владелец подтверждает/правит только существенное;
4. подключает нужный канал/рекламу/оплату;
5. получает готовый стартовый вариант: offer + booking path + creative/content/action;
6. запускает первое действие.

### AI

AI может извлекать/предлагать структуру, но подтверждённый `BusinessProfile` — durable domain state, а не prompt memory.

### DONE когда

Нетехнический пользователь может пройти onboarding без API/provider terminology и получить готовый первый action.

### Evidence

- PR #198 (`U-007: zero-to-first-outcome onboarding`) merged в `main` как `26ea24496ebcca37c5e6e0f04ac4814d5175d965`.
- Все 15 обязательных PR workflows на точном head `25de33dc42b5a97475bb12c306e925628d88d576` завершились `success`, включая Canon, CI, pre-deploy, production isolation, scenario matrix, backup и concurrency contours.
- Coverage-ratchets не снижены, а повышены и зафиксированы на combined `74.30%` и branch `65.33%`; full coverage run: `3638 passed, 7 skipped`.

---

## U-008 — `DONE` — CRM Lead Inbox / Sales Desk

### Цель

Ни один полученный лид не должен теряться после привлечения.

### Каноническая реализация

Current `main` уже содержит sales-domain, поэтому U-008 реализован расширением существующего контура, без нового `crm.py`, отдельного CRM storage или второго источника истины:

```text
clientplatform/domain/sales.py
clientplatform/application/sales_operations.py
clientplatform/infrastructure/sales_repository.py
clientplatform/infrastructure/sales_ui_repository.py
services/db/schema/clientplatform_sales.py
handlers/clientplatform_sales.py
```

Legacy `services/sales_desk*` допускается только как источник поведения, которое нужно сохранить; storage и decision authority остаются в каноническом `clientplatform` sales-контуре.

### Возможности

- lead/customer unified identity через существующий customer/sales контур;
- durable stage/status;
- source + canonical first-touch attribution;
- owner/assignee + assignment/unassignment;
- durable next action + timezone-aware due time;
- notes/audit через существующие `clientplatform_sales_events` с business/lead-scoped dedupe;
- booking/order links через существующие canonical boundaries;
- owner projection для «нужен ответ сегодня» / due work;
- нормализованный WON/LOST closure reason;
- WON необратим;
- LOST перед дальнейшим progression обязан пройти explicit reopen `LOST → NEW`;
- tenant-crossing reads/writes/mutations fail closed.

### DONE когда

Лид от acquisition попадает в понятный owner inbox, проходит stage до booking/payment и сохраняет attribution.

### Evidence

- PR #203 (`U-008: durable sales operations and owner projection`) merged в `main` как `7492ca6f1ac6bd3e00526dac80c6d0cba32ad2cd`.
- Все 15 обязательных PR workflows на точном head `8d46867f2ce0a176b26e8af53e3d2dcea26362b5` завершились `success`, включая Canon, CI quality/coverage, Critical Static Surface, Pre-deploy Release Gate, Production Isolation, Encrypted Backup, User Scenario Matrix и concurrency contours.
- Coverage-ratchet не ослаблен: combined baseline сохранён на `74.30%`, branch baseline повышен до `65.35%`.
- Regression coverage закрепляет tenant crossing, assignment/unassignment, durable next-action/due, lifecycle close/reopen, запрет прямого `LOST → WON`, notes/dedupe и attribution-aware owner projection.

---

## U-009 — `DONE` — Follow-up Employee

### Цель

ClientPlatform напоминает и возвращает лидов, не превращаясь в спам-движок.

### Возможности

- follow-up schedule;
- owner reminder;
- permitted customer message;
- stale lead detection;
- no-response policy;
- stop on reply/booking/payment/opt-out;
- channel-specific consent rules;
- audit trail.

### Требования

- AutomationPolicy/approval governs external messaging;
- quiet hours/timezone;
- opt-out suppression;
- business-scoped idempotency;
- no duplicate send on worker retry/restart.

### DONE когда

Безопасный follow-up работает end-to-end и автоматически прекращается при достижении результата или запрета.

### Evidence

- PR #207 (`U-009: safe follow-up employee`) squash-merged в `main` как `6436e88de24b5b9caa9e06182ff1b190bfb91865`.
- Все 15 PR workflows на точном head `41cf7ecb16f22aca034c9ca1b61a0237ed018d7c` завершились `success`, включая Canon, CI quality/coverage, Critical Static Surface, Pre-deploy Release Gate, Production Isolation, Encrypted Backup, User Scenario Matrix и concurrency contours.
- Coverage ratchets повышены и зафиксированы на `74.56%` combined / `65.58%` branch.
- Canonical production deploy подтверждён на exact SHA `6436e88de24b5b9caa9e06182ff1b190bfb91865`: encrypted backup, `/healthz` + `/readyz`, polling/webhook contract и visual gateway readiness прошли.
- Единый production sales smoke `u008-u009-sales-operations-v2` прошёл U-008 + U-009 checks на PostgreSQL с `rollback_clean=true` и нулевым residue во всех synthetic tables.

---

## U-010 — `DONE` — Retention & Reactivation Engine

### Цель

ClientPlatform должен зарабатывать владельцу не только первым лидом, но и повторными продажами.

### Каноническая реализация первого vertical

U-010 завершён расширением существующих customer, sales, follow-up, outcome и revenue-attribution контуров, без отдельной CRM, sender, outcome ledger или второго retention brain:

```text
clientplatform/domain/retention.py
clientplatform/application/retention.py
clientplatform/infrastructure/retention_repository.py
clientplatform/infrastructure/sales_repository.py
handlers/clientplatform_sales.py
handlers/clientplatform_sales_operations.py
```

### Cohorts первого vertical

```text
one-time customer
inactive customer
```

`no-show`, `stale lead`, `program dropped`, `subscription/payment lapsed` и `high-value returning customer` остаются последующими расширениями только после появления достаточных канонических фактов. Они не должны вычисляться из догадок, prompt memory или параллельного хранилища.

### Возможности

- deterministic cohort builder на canonical customer/outcome facts;
- понятное suggested reactivation action в owner UI;
- повторная server-side проверка exact cohort перед owner-approved mutation;
- materialization в существующий sales lead с `contact_basis=existing_customer` и stable evidence cycle;
- выбор только безопасного активного Telegram/VK/MAX route либо честная ручная работа;
- approval сам ничего не отправляет: внешнее сообщение остаётся за U-009 follow-up/outbox и AutomationPolicy boundary;
- атомарные `order_paid` + `customer_reactivated` outcomes, exact money/currency и существующая revenue attribution;
- business-scoped idempotency, tenant isolation, LOST reopen/WON terminal и stop/cancel follow-up rules.

### Метрики

```text
reactivation rate
repeat purchase rate
retained revenue
incremental reactivation revenue
```

### DONE когда

Можно доказать реальный цикл `inactive customer → action → return → outcome/revenue`.

### Evidence

- PR #208 (`U-010: add deterministic retention cohorts`) merged в `main` как `c87a4de62b6ef931686b39a7bb891fa394d0fa7d`.
- PR #209 (`U-010: add owner-approved reactivation work`) merged в `main` как `6ba76983c255ed70486ce38803c4f3dfd002aa3d`; exact head `8922e217dbfd7a61bb922f3b8a0753344cce2745` включает owner action, outcome/revenue loop и coverage ratchet.
- Все 15 обязательных PR workflows на exact head завершились `success`, включая Canon, CI quality/coverage, Critical Static Surface, Pre-deploy Release Gate, Production Isolation, Encrypted Backup, User Scenario Matrix и concurrency contours.
- Coverage ratchets повышены и зафиксированы на `74.61%` combined / `65.62%` branch.
- Regression coverage доказывает deterministic cohorts, stale/cross-tenant rejection, replay/conflicting-money semantics, exact RUB minor units, atomic rollback, canonical WON evidence и остановку уже запланированного follow-up/outbox после подтверждённого возврата.

---

# 8. Milestones после immediate queue

Immediate queue даёт сквозную коммерческую петлю. После неё проект двигается milestone-ами, каждый из которых должен раскладываться на PR-sized slices перед реализацией.

## M1 — Measure Money

Состав: U-001…U-004.

Exit criteria:

- ClientPlatform знает durable outcomes;
- источник привлечения проходит до клиента/booking/order;
- деньги и реклама соединяются только при доказуемой attribution;
- owner видит честные unit economics.

## M2 — One Click to Customers

Состав: U-005…U-007.

Exit criteria:

- новый владелец быстро создаёт BusinessProfile;
- подключает Yandex/каналы без технического языка;
- ClientPlatform сам обеспечивает managed advertising contour;
- запуск spend только по consent/policy;
- результат виден в Growth Cockpit.

## M3 — Never Lose a Lead

Состав: U-008…U-010.

Exit criteria:

- каждый лид попадает в sales desk;
- follow-up не забывается;
- старые клиенты возвращаются;
- повторная выручка измеряется.

---

# 9. M4 — Revenue Operating System

После M1–M3 ClientPlatform должен стать системой ежедневного управления доходом малого бизнеса.

## 9.1. M4-001 — `DONE` — Customer Payment Evidence Bridge

### Цель

Устранить разрыв между существующим tenant-scoped `business_payments` owner-контуром и каноническим outcome/revenue spine. Подтверждённая оплата клиента специалисту должна становиться одним воспроизводимым денежным фактом, а не отдельной цифрой в админке.

### Первый vertical

```text
active business customer
→ owner/provider payment confirmation
→ one durable business payment
→ one order_paid outcome
→ existing revenue attribution when evidence permits
→ explicit refund_recorded/reversal path
→ owner sees an explainable result
```

### Границы

- расширить существующие `business_payments`, `business_offering_prices` и outcome/revenue modules; перед созданием новых файлов повторно проверить current `main`;
- не смешивать оплату клиента специалисту с legacy/platform subscription payments в `services/payments/*`;
- деньги только integer minor units + ISO currency;
- business-scoped idempotency обязателен для owner retry и provider callback;
- повтор exact request возвращает тот же результат, conflicting replay fail-closed;
- payment/refund state и outcome evidence меняются атомарно либо не меняются совсем;
- customer/offering/payment всегда разрешаются внутри server-authorized business scope;
- provider подтверждает внешний факт, но не становится источником внутренней order/payment policy.

### Tests / gates

- payment happy path и exact replay;
- conflicting replay;
- refund/reversal и запрет double refund;
- transaction rollback without orphan payment/outcome;
- cross-tenant customer/offering/payment rejection;
- concurrent duplicate owner/provider confirmation;
- mixed/unknown currency fail-closed;
- existing U-001/U-003/U-008/U-010 и payment regression contours green.

### DONE когда

Одна подтверждённая клиентская оплата создаёт ровно один tenant-scoped payment и ровно один связанный `order_paid` outcome, безопасно переживает retry/concurrency/restart, а возврат отражается отдельным каноническим денежным фактом без двойного учёта.

### Evidence

- PR #224 (`M4-001a: add canonical customer payment evidence`) merged в `main` как `f5dc4bd0f690ae859852025c240cfedf62389b25`; core связывает tenant-scoped `business_payments` с canonical `order_paid` / `refund_recorded` outcomes и revenue attribution, сохраняя отдельность platform billing.
- PR #213 (`M4-001b: backfill canonical outcomes for legacy payments`) merged в `main` как `c9574dc68a9858d0429436bd2c37e32d00b80eb9`; exact head `044dc7e0a09b8bca9ae504c07a266aa80b557d9e` прошёл required workflows, включая CI quality/coverage, static security, PostgreSQL payment/concurrency, User Scenario Matrix, Production Isolation и Pre-deploy.
- Финальный M4-001 coverage ratchet на #213: combined `74.84%`, branch `65.95%`, без снижения baseline.
- `tests/test_clientplatform_payment_evidence_m4001.py` закрепляет happy/exact replay, conflicting provider evidence, refund/double-refund, atomic rollback, tenant isolation, currency validation, concurrent duplicate confirmation и legacy reconciliation; migration regression и PostgreSQL smoke проверяют backfill/replay.
- Последующий `main` `af21a49683917d8e5d5ac4b5d0f8249f589b1bbd` после PR #228 повторно прошёл все push CI/release/concurrency/isolation contours, подтверждая отсутствие интеграционной регрессии.

### Последующие Commerce / Orders / Payments capabilities

Перед добавлением новых файлов найти current canonical payment/order modules и расширять их.

Требуемые способности:

- offers/products/services;
- order lifecycle;
- invoices/payment links через разрешённых провайдеров;
- payment/refund outcomes;
- recurring/subscription entitlement там, где юридически/технически допустимо;
- taxes/receipts/provider fiscal concerns как explicit provider capability, не скрытая магия;
- currency-safe money model;
- never store unnecessary card data.

## 9.2. M4-002 — `DONE` — Unified Customer Timeline Projection

### Цель

Дать владельцу одну объяснимую историю клиента поверх уже существующих canonical facts, не создавая новый event store, вторую CRM или параллельную платёжную историю.

Первый read-only vertical:

```text
customer
→ first acquisition touch/source
→ sales lead/stage and meaningful sales events
→ booking facts
→ payment/refund outcomes
→ retention/reactivation facts when present
→ one ordered owner timeline
```

### Границы

- сначала расширить существующие customer/activity, attribution, sales, booking, outcome/revenue и retention projections; новый durable timeline storage не добавлять без доказанной необходимости;
- каждая timeline entry обязана ссылаться на canonical source/type/id и не становится новым источником истины;
- business scope и permission разрешаются server-side; чужой `customer_id`, lead, booking, payment или outcome fail-closed;
- порядок событий детерминирован по canonical occurrence time с устойчивым tie-break, без LLM-сортировки;
- отсутствующие facts отображаются как отсутствующие, а не восстанавливаются догадками;
- money entries используют canonical minor units/currency и не суммируют mixed/unknown currency;
- пользовательский текст объясняет бизнес-смысл события и скрывает provider/DB jargon.

### Tests / gates

- timeline happy path минимум из acquisition + sales + booking + payment;
- refund/reversal отображается отдельным денежным фактом и не выглядит второй оплатой;
- deterministic ordering и exact replay без дублей projection rows;
- partial customer history без выдуманных промежуточных шагов;
- cross-tenant customer/source rejection и role permission checks;
- existing U-001/U-002/U-003/U-008/U-009/U-010/M4-001 regressions green;
- coverage ratchet не снижать.

### DONE когда

Owner открывает одного клиента и получает одну tenant-scoped, детерминированную и объяснимую chronology его acquisition/sales/booking/payment history, собранную из существующих canonical facts без второго хранилища бизнес-истины.

### Evidence

- PR #230 (`M4-002: add unified customer timeline projection`) merged в `main` как `b5ef6ef0a583577b6b0d6ba0b9a7ded75b36e049`; exact PR head `28c94e11f55d5c384714ed757098ca2229480243` прошёл все 15 pull-request workflow runs, включая CI quality/coverage, Canon, Critical Static Surface, User Scenario Matrix, PostgreSQL payment/concurrency, booking/ad-spend/partner concurrency, Production Isolation, Encrypted Backup и Pre-deploy Release Gate.
- Timeline остаётся read-only projection поверх canonical customer, attribution, sales и outcome facts; отдельный timeline store/event store/CRM не добавлен.
- Review follow-up закрепил signed refund/reversal semantics, immutable outcome event identity и ISO-4217 minor-unit exponents; dependency-light regressions и focused Telegram/VK/MAX customer-card tests зелёные.

Последующие расширения той же timeline добавляют messages, materials/program progress и support/feedback только через их canonical факты.

## 9.3. M4-003 — `DONE` — Tasks and owner operating queue

ClientPlatform ежедневно собирает максимум несколько действительно важных действий:

```text
ответить горячему лиду
подтвердить изменение рекламного лимита
перенести запись
вернуть клиента
проверить платёж
одобрить контент/кампанию
```

Приоритет объясним и детерминирован бизнес-событиями; LLM может формулировать объяснение, но не скрыто менять приоритеты денег/прав.

### Evidence

- PR #232 (`M4-003: add deterministic owner operating queue`) merged в `main` как `10c1a3043eb75fd1ad2f829fed35be3753733eaf`; exact PR head `6d43c32e1d54d842d309f8d8fa4fa43b6698a441` прошёл все 15 pull-request workflow runs, включая CI quality/coverage, Canon, Critical Static Surface, User Scenario Matrix, PostgreSQL payment/concurrency, booking/ad-spend/partner concurrency, Production Isolation, Encrypted Backup и Pre-deploy Release Gate.
- Owner queue остаётся bounded read-only projection поверх canonical handoff, sales next-action/action-plan и revenue attribution facts; отдельный task store/event store/automation brain не добавлен. Review follow-up закрепил прямую навигацию durable `sales_lead:` actions и безопасный repeatable/FSM escape route `cps:swv:`.
- Coverage ratchet закрыт без ослабления: combined `74.87%`, branch baseline повышен с `66.00%` до `66.02%`.
- Production deploy выполнен на exact merge SHA `10c1a3043eb75fd1ad2f829fed35be3753733eaf`: encrypted backup, внутренние `/healthz` + `/readyz`, публичный HTTPS contract и polling-only Telegram webhook absence прошли; deploy evidence `deploy-20260827T162024Z.json`, stability window `20s` завершился успешно.

## 9.4. Content & funnel operating system

### M4-004 — `DONE` — Owner Content Calendar Projection

Первый следующий vertical: подключить существующий canonical `business_publications` к owner-facing разделу «Публикации» как одну tenant-scoped календарную проекцию по статусам `draft` / `scheduled` / `published` / `failed` / `cancelled`. Использовать уже существующие publication/admin primitives и `scheduled_at`; не создавать отдельный content store, event store или channel-specific source of truth.

Минимальный DONE contract: владелец в Telegram/VK/MAX видит детерминированный список ближайших и недавних публикаций с каналом, статусом и временем; RBAC/tenant isolation сохранены; пустое состояние не выдаёт заглушку о «неподключённом контуре», если canonical publications уже существуют; regression tests и coverage ratchet green. Scheduling/execution/approval workflow остаются последующими отдельными slices.

### Evidence

- PR #234 (`M4-004: add owner content calendar projection`) squash-merged в `main` как `68d736c7e0c5390acb96740c338d0d7a921f225e`; exact PR head `98bd5374b46f985a9c24c560723c8f7d5efe37d2` прошёл все 15 pull-request workflows, включая CI, Canon, Critical Static Surface, User Scenario Matrix, PostgreSQL/concurrency, Production Isolation, Encrypted Backup, Managed Bot Gateway и Pre-deploy Release Gate.
- Один tenant/RBAC-scoped read projection использует существующий `business_publications`: отдельно считает полные статусы, ограничивает upcoming/recent display и не позволяет большой scheduled-очереди скрыть actionable drafts. Telegram, VK и MAX используют общий formatter с business-timezone; отдельный content/event store не добавлен.
- Coverage ratchet закрыт без ослабления и повышен до `74.90%` combined / `66.04%` branch.
- Production deploy выполнен на exact merge SHA `68d736c7e0c5390acb96740c338d0d7a921f225e`: encrypted backup `/var/backups/clientplatform/postgres/clientplatform-20260827T181243Z.dump.age`, внутренние `/healthz` + `/readyz`, публичный HTTPS contract, polling-only Telegram contract и restart-count `0` подтверждены; deploy evidence `deploy-20260827T181702Z.json`.

### M4-005 — `DONE` — Customer Revenue Journey + Money Cockpit

Следующий vertical по прямому решению владельца: собрать уже существующие canonical outcomes, first-touch attribution, booking facts, authoritative payment evidence и retention/reactivation outcomes в один tenant-scoped read model. Новый event store, CRM, payment ledger или AI decision brain не создавать.

Минимальный DONE contract:

```text
source
→ lead
→ booking
→ completed booking
→ paid customer
→ verified revenue
→ reactivated customer
```

- подтверждённая выручка считается из canonical monetary outcomes независимо от полноты attribution; refund/reversal уменьшают её отдельными signed facts;
- отдельно показывается, какая часть выручки доказуемо связана с first-touch source, а какая остаётся без подтверждённого источника;
- source-level projection показывает минимум leads, bookings, completed bookings, paid customers, reactivated customers и currency-safe revenue;
- mixed currencies никогда не суммируются и не используются для ложного рейтинга источников;
- Telegram и native Telegram/VK/MAX показывают один и тот же бизнес-смысл без provider/DB jargon;
- owner surface отвечает на вопросы «сколько заработано / откуда деньги / что сработало / что требует решения / что делать дальше» и не возвращает длинную техническую админку;
- никаких LLM-inferred оплат, источников или revenue.

### Evidence

- PR #237 (`M4-005: add customer revenue journey and money cockpit`) squash-merged в `main` как `08fdb8fc89c6627c4ee3478ed1f3b1a650b79abb`; exact PR head `818d3edd3d1044cef6a805e709e645f1bfd49fac`.
- Все 15 pull-request workflow runs на exact head завершились `success`, включая CI, Canon, Critical Static Surface, User Scenario Matrix, PostgreSQL/concurrency, Production Isolation, Encrypted Backup, Managed Bot Gateway и Pre-deploy Release Gate.
- Все 12 review threads закрыты; regression follow-ups закрепили privacy-detach attribution, ISO-4217 minor-unit formatting, UNKNOWN-source classification, correction/reversal dependency reconciliation и deterministic first-read convergence.
- Production deploy не выполнялся: по Канону он остаётся отдельным действием только по прямому указанию владельца.

### M4-006 — `DONE` — Economic Next Best Action

Поверх M4-005 и существующего M4-003 owner queue добавить детерминированный экономический выбор между уже канонически допустимыми действиями. Первый сценарий: свободные окна + разрешённая reactivation cohort + channel consent + historical outcomes + approved ad-spend limits → понятное предложение владельцу, например сначала использовать бесплатную reactivation, затем только при необходимости предложить paid acquisition в существующих policy limits. LLM может объяснять, но не определяет money/consent/channel constraints.

### Evidence

- PR #239 (`M4-006: add economic next best action`) squash-merged в `main` как `c3a3ac7a47a2663cf04398aff898fb53db8fe744`; exact PR head `099d4a497814887727153f6dce67fcf005306d44`.
- Все 15 pull-request workflows на exact head завершились `success`, включая CI quality/coverage, Canon, Critical Static Surface, User Scenario Matrix, Booking/Ad Spend/Partner concurrency, Production Isolation, Encrypted Backup, Managed Bot Gateway и Pre-deploy Release Gate.
- Два review finding закрыты без обхода Канона: native Telegram/VK/MAX action `Открыть время` ведёт в реальное каноническое создание booking slot, а reactivation routes загружаются одним tenant-scoped bulk query вместо N+1.
- Coverage ratchet усилен до `74.93%` combined / `66.13%` branch; полный локальный regression перед merge: `4081 passed, 7 skipped`.
- Production deploy не выполнялся: по Канону он остаётся отдельным действием только по прямому указанию владельца.

### M4-007 — `DONE` — Owner Publication Scheduling Controls

Вернуть ранее запланированный content vertical после money/NBA slices: дать владельцу безопасно назначать, переносить и отменять время публикации через существующие `business_publications.status` + `scheduled_at`. Не создавать второй scheduler/store и не запускать автоматическую доставку в этом slice.

Минимальный DONE contract: `draft -> scheduled` и controlled reschedule/cancel transitions tenant-scoped и RBAC-защищены; business timezone; прошлое/невалидное время и недопустимые transitions fail-closed; Telegram/VK/MAX дают одинаковые owner actions; duplicate tap/retry идемпотентны. Scheduled execution worker, provider delivery/reconciliation и approval policy остаются последующими slices.

Расширить существующие content/publication/program primitives последовательно: content calendar, reusable assets, cross-channel variants, approval workflow, scheduled publication, evergreen funnels, lead magnets, nurture sequences, conversion outcomes, per-channel compliance/limits и content performance linked to outcomes, а не vanity metrics.

### Evidence

- PR #241 (`M4-007: add owner publication scheduling controls`) squash-merged в `main` как `1637bb565499ec1c3209fed07d5ef30ccefa0aba`; exact PR head `7e6f20a67a10a3932e9b51fa552243f48c0ce520`.
- Все 15 pull-request workflows на exact head завершились `success`, включая CI quality/coverage, Canon, Critical Static Surface, User Scenario Matrix, Booking/Ad Spend/Partner concurrency, Production Isolation, Encrypted Backup, Managed Bot Gateway и Pre-deploy Release Gate; AI Review gate также `pass` по trusted repository policy.
- P1 native VK/MAX retry finding закрыт durable business-scoped idempotency receipt в существующем canonical admin audit contour: stale и первоначально no-op retries возвращают исходный результат, но не могут перезаписать более новое `scheduled_at`; review thread resolved.
- Финальный локальный regression перед merge: `4093 passed, 7 skipped`; coverage ratchet `74.98%` combined / `66.22%` branch при baseline `74.97%` / `66.21%`; Critical Static, Ruff, Canon и release hygiene green.
- Production deploy не выполнялся: scheduled execution/provider delivery по-прежнему не добавлялись и остаются отдельными последующими slices.

### M4-008 — `DONE` — Canonical Outbound Email + External Product Bridge

По прямому указанию владельца добавить универсальный коммерческий bridge перед Safe Autopilot, не создавая второй CRM, sender, scheduler, attribution engine или payment authority. Первый slice состоит только из двух общих capabilities: owner-approved B2B email через существующий `provider_dispatch_outbox` и tenant-scoped signed ingress для проверенных фактов внешних продуктов.

Минимальный DONE contract: SMTP credentials живут только за credential reference и connection активируется после probe; публичный business email не считается согласием и требует OWNER-only approval конкретного recipient + payload, а opt-in/existing relationship сохраняют прежний RBAC; SMTP ambiguity после provider boundary не replay-ится автоматически. External Product Connector включается отдельно, tenant определяется только connector id, HMAC/replay window/body limits/idempotency fail-closed, raw external customer reference не сохраняется, а `lead_created`, `lead_qualified`, `order_paid` и `refund_recorded` материализуются только в существующие customer/outcome/revenue/attribution contours. Никаких product-specific adapters и runtime-зависимостей в этом slice.

---

# 10. M5 — Safe Autopilot

Автопилот — не отдельный «AI-режим», а слой поверх доказанных deterministic capabilities.

### M5-001 — `NEXT` — Canonical AutomationPolicy Foundation

Расширить существующий canonical automation/policy contour, не создавая второй движок или store: формализовать tenant-scoped allowed/forbidden actions, channels/audiences, schedule/quiet hours, approval thresholds, stop conditions, expiry/version и owner approval. Первый slice ограничить policy read/write + deterministic `PolicyCheck`: RBAC и tenant isolation fail-closed, изменение money limits/чувствительных каналов требует явного owner approval, а автономное execution в M5-001 не запускать.

## 10.1. Единый AutomationPolicy

Расширить каноническую модель политики так, чтобы она описывала:

```text
allowed actions
forbidden actions
money limits
AI usage limits
channels
audiences
schedule/quiet hours
content topics/claims
approval thresholds
stop conditions
expiry/version
owner who approved
```

## 10.2. Action lifecycle

Единый шаблон для важных автоматических действий:

```text
Signal
→ CandidateAction
→ PolicyCheck
→ Approval if required
→ Execution
→ Verification
→ AuditRecord
→ Outcome
→ Stop/next action
```

Не создавать отдельный автономный движок для рекламы, CRM, контента и retention, если общий canonical automation layer способен выразить их политики.

## 10.3. Автоматическое улучшение

ClientPlatform может автоматически:

- останавливать явно проигрывающий вариант в утверждённых пределах;
- предлагать новый creative/offer;
- перераспределять разрешённую частоту/очередь действий;
- выбирать лучшее время/канал на основе first-party history;
- возвращать stale leads;
- планировать контент;
- рекомендовать бюджет.

Но увеличение денежных лимитов, новый чувствительный канал, юридически значимое утверждение или действие вне AutomationPolicy требует нового разрешения.

---

# 11. M6 — Omnichannel ClientPlatform

Telegram остаётся первым mature channel, но domain должен быть channel-neutral.

## 11.1. Единая communication model

При необходимости развить canonical entities:

```text
Conversation
Message
ChannelIdentity
ChannelConnection
DeliveryStatus
Consent/OptOut
```

Адаптеры:

```text
Telegram
VK
MAX
Email
SMS
Web chat
other provider only after explicit capability/legal review
```

## 11.2. Правило

Канал не должен становиться отдельной CRM или отдельным automation brain. Один customer/business timeline, разные provider adapters.

## 11.3. Channel rollout gates

Для каждого нового канала:

- auth/connection lifecycle;
- tenant scoping;
- inbound idempotency;
- outbound idempotency;
- retry/rate limiting;
- delivery receipts;
- consent/opt-out;
- attachment handling;
- audit;
- privacy erase/export;
- scenario matrix.

---

# 12. M7 — Full Telegram Mini App / Business Cockpit

`dashboard/` становится полноценной админкой, но Telegram bot остаётся быстрым action surface.

Целевые экраны:

```text
Home / Today
Customers & CRM
Calendar / Booking
Sales / Orders / Payments
Growth / Advertising
Content / Funnels
Programs / Materials
Automation / Approvals
Analytics / Unit Economics
Connections
Team / Roles
Billing
Settings / Privacy
```

## Security requirements

- Telegram initData проверяется backend;
- business scope вычисляется сервером;
- frontend `business_id` никогда не является авторизацией;
- RBAC проверяется API/application layer;
- dangerous actions используют canonical approval/consent;
- no secrets in frontend.

---

# 13. M8 — Billing, Packaging and Monetization

Цены — отдельное продуктовое решение владельца и не должны быть зашиты этим roadmap. Но архитектура должна поддерживать масштабируемую упаковку.

## 13.1. Entitlements instead of scattered flags

Предпочтительные модули после проверки current main:

```text
clientplatform/domain/billing.py
clientplatform/application/billing.py
clientplatform/infrastructure/billing_repository.py
services/db/schema/clientplatform_billing.py
```

Сущности:

```text
Plan
Entitlement
Subscription
UsageMeter
UsageGrant
InvoiceRef
BillingEvent
```

## 13.2. Возможная продуктовая упаковка

Не нормативные названия, а capabilities:

- Starter — core customer/booking/communications;
- Growth — acquisition, CRM, attribution, funnels;
- Autopilot — automation policies, advanced optimization;
- Agency/Partner — multiple businesses, delegation, reporting, partner operations.

Можно иметь trial/free acquisition wedge, но unit economics и abuse-control должны быть измеримы.

## 13.3. Metering

Измерять отдельно:

- AI generation/processing;
- messages/channel costs;
- storage/media;
- managed bots;
- advertising automation actions;
- seats/businesses;
- premium analytics/automation.

Metering не должен мешать бизнес-инвариантам: исчерпание quota fail-closed и объяснимо, без потери данных.

---

# 14. M9 — Partner / Agency / Distribution Platform

Цель — получить масштабируемый канал распространения помимо прямых продаж.

Возможности:

- partner/agency account;
- управление разрешённым набором business workspaces;
- delegated roles без tenant mixing;
- white-label/branding только поверх одного backend;
- partner attribution;
- referral links;
- commission ledger, если бизнес-модель утверждена отдельно;
- portfolio outcome dashboard;
- templates/vertical packs;
- controlled onboarding invitations.

Не давать партнёру глобальный доступ к данным клиентов всех бизнесов по умолчанию.

---

# 15. M10 — Public API, Webhooks and Ecosystem

Только после стабилизации внутренних доменных контрактов.

## API principles

- OAuth/API keys with explicit scopes;
- business-scoped credentials;
- rate limits;
- idempotency keys;
- signed webhooks;
- webhook replay protection;
- event versioning;
- audit logs;
- secret rotation;
- no internal DB models exposed as public contract.

Если отдельного canonical API package ещё нет, допустим целевой namespace `clientplatform/api/`, но перед созданием проверить current main.

## Ecosystem capabilities

- CRM/accounting integrations;
- calendars;
- site/form connectors;
- payment providers;
- analytics export;
- Zapier/Make-like connector only as adapter, not as source of business logic;
- developer portal when external demand exists.

---

# 16. M11 — AI Operating Layer: сильный moat без «второго мозга»

AI должен усиливать ClientPlatform, но не владеть бизнес-истиной.

## 16.1. Возможные canonical entities

После проверки current main:

```text
BusinessContextSnapshot
BrandPolicy
AIProviderCapability
PromptTemplateVersion
AIExecution
AIUsage
AIEvaluation
Recommendation
```

## 16.2. Provider independence

Application layer просит capability:

```text
text_generation
creative_image
summarization
classification
structured_extraction
reasoned_recommendation
speech/audio if introduced
```

Provider adapter выбирается по policy, доступности, стране, цене и quality gate. Никакая продуктовая логика не должна зависеть от одного AI vendor.

## 16.3. Что AI может делать

- извлекать BusinessProfile из текста/сайта/материалов;
- создавать варианты offer/copy/creative;
- суммировать коммуникации;
- классифицировать intent/lead signal;
- предлагать next best action;
- находить причины провала в first-party data;
- создавать content/funnel variants;
- объяснять аналитику человеческим языком.

## 16.4. Что AI не может делать без детерминированного контура

- объявлять платёж подтверждённым;
- менять tenant membership/permissions;
- увеличивать spend cap;
- считать деньги из текста вместо ledger;
- определять факт legal consent;
- незаметно менять AutomationPolicy;
- быть единственным durable memory бизнес-данных.

## 16.5. Evals

Каждый важный AI capability получает versioned evaluation set:

- factuality;
- policy compliance;
- no cross-tenant leakage;
- schema validity;
- refusal/uncertainty behavior;
- Russian language quality + дополнительные целевые языки;
- cost/latency budget.

---

# 17. M12 — Experimentation & Learning System

Чтобы ClientPlatform реально становился лучше, нужны эксперименты, а не только AI-рекомендации.

Сущности/механизмы:

```text
Experiment
Variant
Assignment
Exposure
ConversionOutcome
ExperimentDecision
```

Правила:

- assignment deterministic и business/customer scoped;
- денежные/этические ограничения выше эксперимента;
- не менять одновременно всё без возможности attribution;
- guardrail metrics;
- minimum sample / uncertainty отображаются честно;
- owner can opt out where appropriate.

Применения:

- ad creative;
- landing/offer text;
- follow-up cadence;
- message format;
- booking CTA;
- reactivation offer;
- onboarding sequence.

---

# 18. M13 — Privacy-safe intelligence moat

Главное конкурентное преимущество должно расти от first-party outcomes конкретного бизнеса.

Разрешённые уровни:

1. **Business-local learning:** лучший канал/время/offer именно для этого бизнеса.
2. **Vertical templates:** экспертно/продуктово созданные пакеты без утечки клиентских данных.
3. **Aggregate benchmarks:** только после отдельной privacy architecture/ADR, достаточных групп, suppression thresholds и отсутствия возможности восстановить конкретный business/customer.

Запрещено использовать сырые данные одного бизнеса для улучшения рекомендаций другому без разрешённой privacy/legal модели.

Если нужен vector search/semantic retrieval, это отдельный infrastructure capability с обязательным `business_id` scope; `pgvector` или другой механизм выбирается только после ADR и подтверждённой необходимости.

---

# 19. M14 — Vertical Packs

После появления стабильного общего ядра можно ускорять time-to-value отраслевыми пакетами, не форкая продукт.

Примеры:

- психолог/консультант;
- преподаватель/эксперт;
- beauty/service business;
- автосервис;
- окна/ремонт/локальные услуги;
- digital products/course creator;
- small clinic only after отдельной compliance review.

Vertical Pack может включать:

```text
BusinessProfile defaults
services/offers templates
booking rules
lead stages
content templates
funnel recipes
analytics goals
automation policies
compliance hints
```

Никаких отдельных codebases/servers на вертикаль.

---

# 20. M15 — Trust, Compliance and Enterprise-grade Safety

Массовый продукт не становится большим без доверия.

Обязательные направления:

- data inventory;
- retention policies;
- privacy export/erasure;
- provider/vendor inventory;
- secret rotation;
- least privilege;
- audit immutability for sensitive actions;
- admin/support impersonation policy;
- incident response runbook;
- threat modeling для advertising/payments/messaging/managed bots/API;
- dependency/SBOM discipline;
- security regression and periodic penetration testing;
- backup restore drills;
- RPO/RTO definitions and evidence;
- jurisdiction/provider-specific compliance documented as capabilities, not assumptions.

Никогда не обещать в UI юридическую «полную совместимость», если она не доказана для конкретной юрисдикции/сценария.

---

# 21. M16 — Scale 10k → 100k businesses

Оптимизация только по измерениям, но архитектура заранее не должна закрывать рост.

Направления:

- PostgreSQL query/index telemetry;
- worker queues / leases;
- transactional outbox/inbox;
- rate-limit coordination;
- provider circuit breakers;
- backpressure;
- dead-letter/reconciliation tooling;
- tracing business operation → external provider → outcome;
- per-business cost attribution;
- horizontal workers;
- object storage/media lifecycle;
- read replicas/partitioning only after evidence;
- RLS для high-risk tables там, где усиливает defence in depth;
- SLO/error budgets;
- deploy canaries/staged rollout when scale warrants.

Запрещено преждевременно дробить систему на микросервисы без доказанной bottleneck/ownership необходимости.

---

# 22. M17 — International / provider portability

ClientPlatform должен быть способен менять providers, не переписывая продукт.

Абстракции должны быть capability-based для:

- AI;
- advertising;
- messaging;
- payments;
- email/SMS;
- storage;
- analytics export.

Business/domain layer хранит смысл, provider layer — внешний контракт.

Для новой страны/рынка нужны explicit capability matrix:

```text
provider availability
currency
payments/tax/fiscal constraints
messaging/channel availability
advertising provider
privacy/legal requirements
language/localization
support/operations cost
```

Не делать глобальный launch только переводом строк UI.

---

# 23. UX laws для всех будущих slices

1. Основной интерфейс говорит о результате, не об API.
2. Сначала ClientPlatform предлагает готовый безопасный вариант, потом расширенные настройки.
3. Технические ID скрыты, если они не нужны пользователю для реального решения.
4. Любое важное автоматическое действие объяснимо: что, почему, стоимость/риск, как остановить.
5. Ошибка провайдера переводится в понятное действие, но diagnostic code сохраняется для operations.
6. Owner никогда не должен пересылать access token/secret вручную в чат.
7. Active business всегда видим там, где возможна путаница.
8. Деньги, approvals и dangerous actions имеют явный confirmation/policy boundary.
9. Нельзя требовать от владельца повторно вводить данные, которые ClientPlatform уже надёжно знает.
10. Основной happy path должен становиться короче по мере роста внутренней сложности платформы.
11. На основном операционном экране должна быть не более чем одна визуально главная кнопка: она ведёт к наиболее важному следующему результату из canonical facts. Остальные функции раскрываются вторым уровнем через «Все возможности» / «Другие действия».
12. Progressive disclosure не имеет права удалять функциональность: каждая ранее доступная разрешённая возможность остаётся достижимой, а money/privacy/approval boundaries не прячутся и не ослабляются.

---

# 24. Engineering laws для всех будущих slices

## Domain before adapter

Сначала определить business meaning/invariants, потом подключать provider.

## One canonical path

Если обнаружены две функции/обработчика, которые реализуют один и тот же пользовательский смысл разными правилами, консолидировать источник истины вместо синхронизации копий.

## No second brain

- LLM не является БД.
- prompt не является policy.
- provider campaign не является внутренней PromotionCampaign.
- Telegram callback не является авторизацией.
- frontend state не является tenant identity.

## Fail closed where value can be lost

Особенно:

- деньги;
- permissions;
- privacy;
- external writes;
- advertising launch;
- deletion;
- attribution used for financial decisions.

## Idempotency is a product property

Duplicate tap, retry, worker restart или uncertain provider response не должны создавать двойную оплату, двойной spend, два managed bots, две managed campaigns или повторную customer communication.

## Evidence-driven DONE

Тест, CI и merged code важнее отчёта в чате.

---

# 25. Definition of DONE для каждого slice

Перед переводом строки roadmap в `DONE` проверить:

- [ ] domain invariants;
- [ ] DB schema/constraints/indexes/migration, если нужны;
- [ ] tenant scope and RBAC;
- [ ] application orchestration;
- [ ] provider adapter boundaries;
- [ ] result-first UX;
- [ ] idempotency/concurrency/restart semantics;
- [ ] ambiguous external result/reconciliation;
- [ ] audit/observability;
- [ ] privacy manifest/export/erasure impact;
- [ ] happy-path test;
- [ ] fail-closed test;
- [ ] cross-tenant test;
- [ ] concurrency/restart test where relevant;
- [ ] money/currency test where relevant;
- [ ] Canon validator and existing gates remain green;
- [ ] coverage ratchet not lowered;
- [ ] PR merged to `main`;
- [ ] roadmap evidence updated with PR, merge SHA and meaningful checks.

Если хотя бы критический applicable пункт не выполнен, статус не `DONE`.

---

# 26. Как следующий чат обязан работать с roadmap

1. Прочитать `AGENTS.md`.
2. Проверить актуальный `main`, open PR и CI.
3. Прочитать Канон полностью.
4. Прочитать этот roadmap полностью.
5. Если владелец дал конкретную задачу — приоритет у неё.
6. Иначе брать **только текущий `NEXT`**.
7. Перед созданием нового файла искать existing canonical implementation.
8. Реализовать минимальный полноценный vertical slice, а не сразу весь milestone.
9. Добавить regression tests.
10. Открыть один PR.
11. Исправлять реальные CI-проблемы, а не ослаблять gates.
12. Merge только зелёный PR.
13. После merge обновить roadmap: текущий slice → `DONE` + evidence; следующий первый `QUEUED` → `NEXT`.
14. Не начинать следующий slice в том же PR, если он не необходим для завершения текущего контракта.
15. Production deploy — отдельная работа только по прямой команде владельца.

Если live-provider проверка невозможна без production credentials:

- закончить код и deterministic tests;
- не просить секрет в ChatGPT;
- отметить `BLOCKED-LIVE` только там, где live evidence действительно является незакрытым критерием;
- записать безопасный exact validation procedure.

---

# 27. Roadmap evidence log

Эта секция обновляется только после merge.

| Slice | Status | Evidence |
|---|---|---|
| Multitenant / Telegram / production foundations | DONE | baseline до v16.1; фактический current main проверять перед работой |
| Canonical acquisition navigation | DONE | PR #158–#159, merged before baseline anchor |
| Yandex managed campaign lifecycle | DONE | PR #160; merge SHA `de0c332e0f4ed3bea408b7da4319cda04da58a69`; all PR and post-merge main gates green |
| U-001 Durable Outcome Ledger | DONE | PR #170; merge SHA `85f676db72e57e9281b04dd623291d087c1b4d56`; all PR workflows green including Canon, CI quality/coverage, static security, PostgreSQL and booking concurrency, production isolation and pre-deploy |
| U-002 Acquisition Attribution Spine | DONE | PR #172; merge SHA `c7f85182e3d100c82de87c996b34ab5e71fbf31b`; all 15 PR workflows green including Canon, CI quality/coverage, static security, booking/ad-spend/partner concurrency, production isolation, user scenario matrix and pre-deploy |
| U-003 Revenue Attribution & Unit Economics | DONE | PR #174; merge SHA `148bc3ba1732cbe530973d20284308cc16b6df70`; all 15 PR workflows green including Canon, CI quality/coverage, static security, booking/ad-spend/partner concurrency, production isolation, user scenario matrix and pre-deploy |
| U-004 Yandex Campaign-level Read Analytics | DONE | PR #177; merge SHA `c5ad2187402593152688cc90a60f559830f148f6`; all 15 PR workflows green including Canon, CI quality/coverage, static security, booking/ad-spend/partner concurrency, production isolation, user scenario matrix and pre-deploy; coverage baseline 74.16% combined / 65.22% branch |
| U-005 Managed Yandex Activation Policy | DONE | PR #194; merge SHA `d5a518167515df1cab5086f24aaf2eddcff1f1ff`; all 15 PR workflows green including Canon, CI quality/coverage, static security, ad-spend/booking/partner concurrency, production isolation, user scenario matrix and pre-deploy |
| U-006 Growth Cockpit | DONE | PR #196; merge SHA `65472747f9b0ed6f2941b21309a16f4f6c426c5d`; all 15 required PR workflows success on `dc83b988b33b933d3a248d2b16ab0cc71ebe8217`; coverage ratchets preserved at 74.21% combined / 65.28% branch |
| U-007 Zero-to-First-Outcome Onboarding | DONE | PR #198; merge SHA `26ea24496ebcca37c5e6e0f04ac4814d5175d965`; all 15 required PR workflows success on `25de33dc42b5a97475bb12c306e925628d88d576`; coverage ratchets raised to 74.30% combined / 65.33% branch |
| U-008 CRM Lead Inbox | DONE | PR #203; merge SHA `7492ca6f1ac6bd3e00526dac80c6d0cba32ad2cd`; all 15 required PR workflows success on `8d46867f2ce0a176b26e8af53e3d2dcea26362b5`; combined coverage baseline 74.30%, branch baseline raised to 65.35%; canonical sales contour extended without `crm.py` or second sales storage |
| U-009 Follow-up Employee | DONE | PR #207; merge SHA `6436e88de24b5b9caa9e06182ff1b190bfb91865`; all 15 PR workflows green; production smoke `u008-u009-sales-operations-v2` rollback-clean |
| U-010 Retention & Reactivation Engine | DONE | PR #208 merge SHA `c87a4de62b6ef931686b39a7bb891fa394d0fa7d`; PR #209 merge SHA `6ba76983c255ed70486ce38803c4f3dfd002aa3d`; all 15 required workflows success on exact head `8922e217dbfd7a61bb922f3b8a0753344cce2745`; coverage raised to 74.61% combined / 65.62% branch; canonical sales/follow-up/outcome/revenue contours extended without a second retention brain |
| M4-001 Customer Payment Evidence Bridge | DONE | PR #224 merge `f5dc4bd0f690ae859852025c240cfedf62389b25`; PR #213 merge `c9574dc68a9858d0429436bd2c37e32d00b80eb9`; final #213 required workflows green at 74.84% combined / 65.95% branch; current `main` `af21a49683917d8e5d5ac4b5d0f8249f589b1bbd` post-merge contours green |
| M4-002 Unified Customer Timeline Projection | DONE | PR #230 merge `b5ef6ef0a583577b6b0d6ba0b9a7ded75b36e049`; exact PR head `28c94e11f55d5c384714ed757098ca2229480243`; all 15 PR workflows success; read-only canonical customer timeline with tenant/RBAC isolation, Telegram/VK/MAX surfaces and corrected refund/reversal + ISO-4217 money semantics |
| M4-003 Tasks and owner operating queue | DONE | PR #232 merge `10c1a3043eb75fd1ad2f829fed35be3753733eaf`; exact PR head `6d43c32e1d54d842d309f8d8fa4fa43b6698a441`; all 15 PR workflows success; coverage 74.87% combined / 66.02% branch; exact-SHA production deploy with encrypted backup, health/readiness, HTTPS and polling-only stability evidence |
| M4-004 Owner Content Calendar Projection | DONE | PR #234 merge `68d736c7e0c5390acb96740c338d0d7a921f225e`; exact PR head `98bd5374b46f985a9c24c560723c8f7d5efe37d2`; all 15 PR workflows success; coverage 74.90% combined / 66.04% branch; exact-SHA production deploy with encrypted backup, health/readiness, HTTPS, polling-only and restart=0 evidence |
| M4-005 Customer Revenue Journey + Money Cockpit | DONE | PR #237 squash-merge `08fdb8fc89c6627c4ee3478ed1f3b1a650b79abb`; exact head `818d3edd3d1044cef6a805e709e645f1bfd49fac`; all 15 PR workflows success; 12 review threads resolved; canonical outcome/attribution/payment/reactivation projection with Telegram/VK/MAX parity and no second store/brain |
| M4-006 Economic Next Best Action | DONE | PR #239 squash-merge `c3a3ac7a47a2663cf04398aff898fb53db8fe744`; exact head `099d4a497814887727153f6dce67fcf005306d44`; all 15 PR workflows success; coverage raised to 74.93% combined / 66.13% branch; native slot creation and bulk reactivation routing review findings resolved |
| M4-007 Owner Publication Scheduling Controls | DONE | PR #241 squash-merge `1637bb565499ec1c3209fed07d5ef30ccefa0aba`; exact head `7e6f20a67a10a3932e9b51fa552243f48c0ce520`; all 15 PR workflows success; P1 native VK/MAX stale/no-op retry race fixed with canonical durable idempotency receipt; coverage 74.98% combined / 66.22% branch |
| M4-008 Canonical Outbound Email + External Product Bridge | DONE | PR #243 squash-merge `64e96d13d2e33eede100646d937d45ba947c297f`; exact PR head `656672b20b2b4b3257629f7e70f3635e36d4f99b`; all 15 PR workflows success; focused 61 tests + full regression `4131 passed, 7 skipped`; coverage raised to 74.99% combined / 66.25% branch; production deploy intentionally not part of the slice |
| M5-001 Canonical AutomationPolicy Foundation | NEXT | extend one canonical policy contour with fail-closed tenant/RBAC boundaries, explicit owner approvals and deterministic PolicyCheck; no second automation engine/store and no autonomous execution in this slice |

---

# 28. Финальная продуктовая цель

ClientPlatform считается пришедшим к целевой форме не тогда, когда в нём будет много интеграций, а когда типичный малый бизнес сможет сказать:

> «Я подключил ClientPlatform, рассказал, чем занимаюсь, и дальше он реально помогает мне находить клиентов, не терять их, продавать, возвращать, выдавать материалы/услуги, считать деньги и сам делать безопасную рутину. Я вижу, что он сделал, сколько это принесло и могу в любой момент изменить пределы или остановить его.»

Техническая сложность должна расти **внутри** платформы. Внешний пользовательский сценарий должен становиться проще, короче и результативнее.

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

### M5-001 — `DONE` — Canonical AutomationPolicy Foundation

Расширить существующий canonical automation/policy contour, не создавая второй движок или store: формализовать tenant-scoped allowed/forbidden actions, channels/audiences, schedule/quiet hours, approval thresholds, stop conditions, expiry/version и owner approval. Первый slice ограничить policy read/write + deterministic `PolicyCheck`: RBAC и tenant isolation fail-closed, изменение money limits/чувствительных каналов требует явного owner approval, а автономное execution в M5-001 не запускать.

### Evidence

- PR #245 (`M5-001: add canonical AutomationPolicy foundation`) squash-merged в `main` как `0c813605c23e1d8e6f1f5d4c7f85193a9b09a209`; exact final PR head `536d8e35429c8695f110c09f345fd67303c39d85`.
- Все 16 pull-request workflows на final head завершились `success`, включая CI quality/coverage, Canon, Critical Static Surface, User Scenario Matrix, Booking/Ad Spend/Partner/AutomationPolicy concurrency, Production Isolation, Encrypted Backup, Managed Bot Gateway и Pre-deploy Release Gate; AI Review gate также `pass` по trusted repository policy.
- Два P1 review finding закрыты deterministic invariants/regressions: money-bearing action semantics и обязательный money limit больше нельзя понизить caller payload-ом, а pre-M5 `autopilot_enabled=true` сохраняет только read-only advisory UX без поддельного owner approval; оба review thread resolved.
- Focused AutomationPolicy suite: `14 passed`; финальный full coverage regression: `4151 passed, 7 skipped`; coverage ratchets подняты до `75.00%` combined / `66.26%` branch при baseline `74.99%` / `66.25%`.
- M5-001 не запускает autonomous execution/provider writes: текущий owner toggle разрешает только `growth.read_only_analysis`; production deploy намеренно не выполнялся.
- Следующий исполнимый шаг декомпозирован из уже существующего §10.2 Action lifecycle как M5-002: durable approval boundary перед любым Execution/provider write.

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

### M5-002 — `DONE` — Canonical Action Approval Boundary

Расширить тот же canonical automation contour следующим минимальным шагом после M5-001: сохранять tenant-scoped CandidateAction + PolicyCheck как immutable approval intent и давать OWNER возможность явно approve/reject действие, когда PolicyCheck требует подтверждение. Approval должен быть привязан к exact candidate fingerprint, AutomationPolicy id/version/hash и expiry, быть idempotent/restart-safe и fail-closed при stale/changed policy, cross-tenant доступе или повторном conflicting решении. Использовать существующий canonical audit contour и текущий owner admin surface; не создавать второй approval engine/store, отдельный scheduler или provider-specific automation brain.

Минимальный DONE contract M5-002: read/list pending approvals + approve/reject/revoke, deterministic authorization artifact для последующего Execution slice, owner-only mutation и business-scoped read permissions, immutable audit/evidence, concurrency regression. **Execution, provider calls, autonomous scheduling и money movement в M5-002 не запускать.**

### Evidence

- PR #247 (`M5-002: add canonical action approval boundary`) squash-merged в `main` как `1cbee98d85131c0e6579e292e8413a5ae71b7613`; exact final PR head `6b985a35d73244bebcde56edcc4905efc19e2398`.
- На exact final head все PR checks green: оба CI quality/static контура, Canon, User Scenario Matrix, AutomationPolicy/Ad Spend/payment/booking/bot/partner PostgreSQL concurrency, Production Isolation, Encrypted Backup, Managed Bot Gateway, Pre-deploy Release Gate и AI Review gate.
- Два review finding закрыты fail-closed invariants и regressions: P1 требует для каждого external-write approval exact `subject_ref` + SHA-256 `payload_digest` в candidate/authorization и повторно сверяет их перед выдачей authorization; P2 приоритизирует pending approvals до LIMIT в repository и owner UI. Оба review thread resolved.
- Focused M5 suite после review fixes: `33 tests OK`; финальный full coverage regression: `4172 passed, 7 skipped, 33 warnings`; coverage ratchets `75.02%` combined / `66.29%` branch при locked baseline `75.02%` / `66.29%`.
- PostgreSQL AutomationPolicy concurrency подтверждает restart-safe/idempotent request replay и approve-vs-reject race; stale policy, expiry, cross-tenant и conflicting decisions остаются fail-closed.
- M5-002 не выполняет provider calls, autonomous scheduling, external execution или money movement; production deploy намеренно не выполнялся.
- После M5-002 дальнейший scope был отдельно декомпозирован через issue #263. Исторические M6-статусы ниже читаются по evidence каждого slice, а единственный актуальный `NEXT` определяется только текущей status/evidence секцией roadmap, чтобы старый текст не переопределял более позднее закрытие работ.

## 10.2.1. M6 — Platform parity и безопасный operator/support contour

### M6-001 — `DONE` — Platform Operator Read-Only Snapshot

Создать один read-only platform-owner contour поверх уже существующих canonical owners для release contract, disaster recovery и resource telemetry. Доступ должен fail-close через отдельную high-trust `is_platform_admin` границу до чтения защищённых источников; tenant/business роли не дают platform-level доступ. Snapshot не содержит business/customer records, по умолчанию не делает provider probe и доступен оператору через существующий Telegram entry router без появления в публичном command menu.

### Evidence

- PR #264 (`M6-001: add platform operator read-only snapshot`) merged в `main` как `6acdcb62f6b644592aa23301ad1763a747b3b712`; exact final PR head `c2f32fa2914245c732a8bc06d32d334bcb1454fd`.
- На exact final head все обязательные PR checks green: Canon, CI quality/coverage + static security, User Scenario Matrix, AutomationPolicy/Ad Spend/Booking/Partner concurrency, Managed Bot Gateway, Bot Provisioning, Production Isolation, Encrypted Backup, Pre-deploy Release Gate, Boundary Diagnostics, Brand Gate и AI Review gate.
- Full CI: `3070 passed, 7 skipped`; coverage ratchet повышен и зафиксирован на `81.99%` combined / `73.64%` branch.
- Review P1 «snapshot не подключён к operator surface» закрыт на той же ветке: hidden `/platformstatus` делегирует в canonical `platform_operator_snapshot`; regression доказывает deny-before-read для неавторизованного пользователя и authorized presentation path. Review thread resolved.
- Production deploy выполнен на exact merge SHA `6acdcb62f6b644592aa23301ad1763a747b3b712`: encrypted backup `/var/backups/clientplatform/postgres/clientplatform-20260902T151233Z.dump.age`, deploy evidence `deploy-20260902T151537Z.json`, internal `/healthz` + `/readyz` green, внешний `https://app.clientplatform.ru/` возвращает exact `ClientPlatform`, публичные health/readiness и Telegram webhook остаются `404`, app `restart_count=0`, stability window `20s` завершился успешно.

### M6-002 — `DONE` — Audited Support Access Session

Platform-support получает безопасный, ограниченный по времени доступ к **одному явно выбранному business** через отдельную audited support session, не превращая SUPPORT/tenant role в platform-admin и не создавая второго auth/RBAC/store. Сессия привязана к platform operator, exact `business_id`, обязательной причине/ticket reference, `issued_at`, `expires_at`, явному revoke и durable audit evidence. Доступ read-only.

### Evidence

- PR #266 (`M6-002: add audited platform support sessions`) merged в `main` как `ddc0bd67ad9e7be549e6c5a840af36f8e5f2a402`; exact final PR head `2e56a1498526b76526a8516cdf5badba3e88f1ae`.
- Все 16 workflow на exact head завершились `success`: CI, Canon, User Scenario Matrix, Critical Static Surface, AutomationPolicy/Ad Spend/Booking/Partner concurrency, Managed Bot Gateway, Bot Provisioning, Production Isolation, Encrypted Backup, Pre-deploy Release Gate, Boundary Diagnostics, Brand Gate и Runner Diagnostic.
- GitHub commit statuses green: regression contour; combined coverage `82.08%` при ratchet baseline `82.09%` в разрешённой tolerance `0.01`; branch coverage `73.73% / 73.73%`. Локальный полный coverage-прогон перед push: `3088 passed, 7 skipped`, combined `82.09%`, branch `73.73%`; baseline не снижался.
- Critical static manifest расширен до `102` type-critical файлов и `111` security paths; exact-head mypy, Bandit и dependency audit green.
- Изолированный PostgreSQL 16 smoke подтвердил restart-safe/idempotent contour: 12 concurrent identical issue requests дали одну durable session и один `issued` audit; allowed read + revoke прошли; `business_members` не изменился. Read-vs-revoke сериализован на exact capability row.
- Hidden `/supportsession open|read|revoke` не добавлен в публичное command menu. Session не создаёт membership и synthetic `TenantContext`; business metadata читаются через canonical `TenancyRepository`; expiry/revoke/cross-business/operator mismatch fail-close.
- Inline review threads на final head отсутствовали. Repository-side `AI Review / gate` был green с явным статусом `L3 external AI review temporarily disabled by trusted repository policy`; внешний Codex review не выдаётся за выполненный, поскольку code-review quota была исчерпана.
- Production deployment **не входил** в M6-002 merge/evidence и этим roadmap-closure не утверждается.

### M6-003 — `DONE` — Support Case Intake + Operator Queue

Canonical support-case lifecycle позволяет tenant явно создать обращение, а platform operator — работать с cross-tenant очередью **только существующих support cases**, не получая каталог всех businesses/users и не приобретая tenant-доступ от одного факта claim.

### Evidence

- PR #268 (`M6-003: add canonical support case operator queue`) merged в `main` как `5cc038e7b2a6a617e2a07ecfb223d580f4e48ec0`; exact final PR head `07504bf975fcc23ecdf65c793aa9b040d648dc7f`.
- На exact final head все 16 обязательных workflow завершились `success`; `AI Review / gate` green. Два P1 review finding исправлены до merge: operator queue теперь chunked/bounded ниже Telegram message limit, а secret filter блокирует credential-like natural-language forms без ложного запрета обычных фраз. Threads resolved.
- Final GitHub coverage ratchet зафиксирован без снижения baseline: `82.19%` combined / `73.93%` branch. PostgreSQL/static-security/regression contour и concurrency gates green.
- Business-scoped cases создаются через один channel-neutral application owner; Telegram/VK/MAX не получили отдельные stores/queues. Tenant isolation, strict idempotency/stale replay fail-close и atomic claim/release/resolve покрыты regression tests.
- Claim case не создаёт membership или synthetic `TenantContext`. Business inspection требует отдельную M6-002 support session, выдаваемую для exact case/business в той же canonical DB transaction при lock exact claimed case.
- Production deploy выполнен на exact merge SHA `5cc038e7b2a6a617e2a07ecfb223d580f4e48ec0`: encrypted backup `/var/backups/clientplatform/postgres/clientplatform-20260902T200530Z.dump.age`, deploy evidence `/var/lib/clientplatform/deploy-evidence/deploy-20260902T200829Z.json`, `CLIENTPLATFORM_UPDATE_STABILITY_OK:20s`, все production containers `running` с `restart_count=0`, internal `/healthz` + `/readyz` = `200`, внешний `https://app.clientplatform.ru/` = `200 ClientPlatform`, публичные health/readiness/webhook остаются `404`.

### M6-004 — `DONE` — Bounded Platform Directory Search + Access Review

Platform operator получил query-bound служебную навигацию к конкретному business/account без глобального tenant role, bulk tenant browsing или права читать business data без отдельной M6-002 support session.

### Evidence

- PR #273 (`M6-004: add bounded platform directory search`) merged в `main` как `6b21f928272e97db5b8b79bb3adc37db62bf5798`; exact final PR head `33f9679907be60c26f7474bcf709367bef456f84`.
- На exact final head все 16 workflow завершились `success`; repository-side `AI Review / gate` green. External L3 review на этом запуске был отключён trusted repository policy и не выдаётся за выполненный.
- Два review finding исправлены до merge: active memberships приоритизируются перед revoked history с явным `truncated`, а business-name lookup использует одинаковый case-normalized contract в SQLite и PostgreSQL. Оба review thread resolved.
- Full CI: `3138 passed, 7 skipped`; coverage ratchet сохранён без ослабления на `82.23%` combined / `73.99%` branch. PostgreSQL platform-directory smoke, static security и regression contour green.
- Hidden `/platformdirectory business|user|name` использует canonical tenancy/identity owners, не создаёт membership/synthetic `TenantContext`, не меняет roles и не раскрывает customer/payment/provider data; lookup audit append-only и query-bound.
- Production deploy выполнен на exact merge SHA `6b21f928272e97db5b8b79bb3adc37db62bf5798`: encrypted backup `/var/backups/clientplatform/postgres/clientplatform-20260903T052202Z.dump.age`, deploy evidence `/var/lib/clientplatform/deploy-evidence/deploy-20260903T052535Z.json`, `CLIENTPLATFORM_UPDATE_STABILITY_OK:20s`, restart count `0`, internal `/healthz` + `/readyz` = `200`, внешний root = `ClientPlatform`, публичные health/readiness/Telegram webhook = `404`.
- Первый deploy attempt fail-closed остановился до production mutation из-за disk-capacity guard. После удаления только воспроизводимых build/package caches и obsolete ClientPlatform image history повторный canonical deploy прошёл без обхода guard; current runtime images, последняя rollback-пара, production volumes и encrypted backups сохранены. После post-deploy build-cache/image-retention cleanup фактическое использование root filesystem `74.331%`, ниже hard gate.

### M6-005 — `DONE` — Disk-safe Production Deploy Retention

Canonical production deploy теперь различает тяжёлый `full_runtime` rollout и доказанный `host_only_noop`, сохраняет encrypted backup/health/smoke/evidence во всех режимах и не вынуждает host-only governance changes бессмысленно пересобирать runtime images.

### Evidence

- Основной slice: PR #275 merged как `93c2d5b99cb5cc12d3b31d081d4adb6a71149613`; corrective production-acceptance PR #276 merged как `be6d3e743e0f85ee8cd49c84ad2c8b09b730ad74`; host-only rollout PR #277 merged как `1b228ae2f843483364a4e9ba324a5a93a92899b0`; финальный rollout-aware disk contract PR #278 merged как `48ee169cc72e687d0d8d2e89fbbe748eca41fd8b`.
- Финальный PR #278 exact head `7e46785dc73762e2b8610f7cec25edb6afb784d8`: все 16 workflow `success`; `AI Review / gate`, regression contour и coverage ratchets green; coverage удержан на `82.23%` combined / `73.99%` branch.
- Реальные production acceptance failures #275/#276/#277 не скрывались: hard guard дважды остановил deploy до mutation, #276 full-runtime rollout был безопасно rollback после post-deploy disk gate. Эти результаты использованы для исправления root cause, а не для ослабления full-runtime guard.
- `full_runtime` hard contract сохранён без изменения: `<75%` root used и `>=7 GiB` free. Только доказанный по latest successful evidence + git ancestry + narrow allowlist `host_only_noop` получает отдельный conservative no-build contract `<85%` / `>=4 GiB`; неизвестный/unproven diff всегда становится `full_runtime`.
- Host-only deploy не выполняет runtime `build`/`--force-recreate`, но сохраняет canonical encrypted backup, baseline/visual readiness, external HTTPS + polling isolation, sales operations smoke, project-scoped retention и immutable deploy evidence. Глобальные image/volume/system prune запрещены.
- Финальный exact-SHA production acceptance `48ee169cc72e687d0d8d2e89fbbe748eca41fd8b` стартовал с `75.487%` used / `6.812 GiB` free — то есть ниже full-runtime headroom — и завершился `CLIENTPLATFORM_PRODUCTION_DEPLOY_OK:/var/lib/clientplatform/deploy-evidence/deploy-20260903T115734Z.json` + `CLIENTPLATFORM_UPDATE_STABILITY_OK:20s`.
- Evidence: `runtime_rollout_mode=host_only_noop`, encrypted backup `/var/backups/clientplatform/postgres/clientplatform-20260903T115730Z.dump.age` (AGE header подтверждён в named backup volume), sales smoke `ok=true`, `disk_before_deploy=75.50%`, `disk_after_deploy=76.99%`, `capacity_ready=true` для host-only contract.
- Все пять production container ID после deploy **точно совпали** с pre-deploy ID; app/visual/provider/PostgreSQL/Caddy restart count `0`. Internal `/healthz` и `/readyz` = `200`; внешний root = `200 ClientPlatform`; public health/readiness/Telegram webhook = `404`. Runtime не пересобирался и не перезапускался.

### M6-006 — `DONE` — Versioned Capability Parity Matrix + Regression Guard

Следующий и единственный default slice превращает требование issue #263 о capability parity с закреплённым donor baseline в versioned executable contract, а не ручной список и не сравнение названий кнопок.

Минимальный DONE contract M6-006:

- на актуальном `main` ClientPlatform и закреплённом в issue #263 donor baseline построить полную матрицу всех 17 обязательных capability families;
- каждая строка/под-capability имеет ровно один статус: `equivalent`, `genericized`, `missing`, `domain-specific`; неопределённый/пустой статус запрещён;
- `equivalent` и `genericized` обязаны ссылаться на canonical ClientPlatform owner/surface и конкретный regression evidence/test; документация без исполняемого доказательства не считается паритетом;
- `domain-specific` обязан явно выделять переносимый generic mechanism либо доказать отсутствие полезного общего поведения; branding/therapy runtime/secrets не переносятся;
- `missing` не маскируется и не закрывается пустой кнопкой: каждая такая capability становится явным gap с owner decision и последующим отдельным vertical slice либо явно согласованным исключением владельца;
- machine-readable parity manifest хранится versioned в repository и проверяется CI guard: все 17 families присутствуют, evidence paths/tests существуют, duplicate/unknown status запрещены, удаление уже доказанной capability ломает guard;
- guard не импортирует/не запускает donor runtime и не создаёт cross-repository runtime dependency; donor snapshot фиксируется только как provenance/evidence для сравнения;
- platform-operator и business-owner capabilities остаются разными уровнями; matrix не может легализовать global TenantContext/superuser или второй store/brain;
- Telegram/VK/MAX parity оценивается как одно canonical application/domain поведение с adapter surfaces, а не как три независимых реализации;
- после M6-006 merge следующий `NEXT` выбирается из первого подтверждённого `missing` operational/platform gap по risk/value, не из косметического меню.

### Evidence

- PR #280 (`M6-006: add executable capability parity contract`) squash-merged в `main` как `8ffa7699c399aa613e64fffcba2448182e0542fe`; exact final PR head `2ef607e976046a1833d7bf31fa58714776c66abb`.
- На exact final head все 17 PR workflow завершились `success`; Independent AI Review / gate также `success`, unresolved review threads = `0`. Canon, Product Purity, Critical Static Surface, User Scenario Matrix, PostgreSQL concurrency, Production Isolation, Encrypted Backup и Pre-deploy Release Gate не ослаблялись.
- Исполняемый manifest покрывает все 17 capability families и 20 capability rows: `2 equivalent`, `17 genericized`, `1 missing`, `0 domain-specific`. Все 19 доказанных `equivalent/genericized` capability входят в hard ratchet; новая proven capability без явного ratchet-update, downgrade или silent removal ломают guard.
- Pinned evidence baseline зафиксирован exact SHA `f63b44dd8963c1e6fd71ae8b05b9028d61f172ad`, root tree `94c7b3fd47b69e7d819a03e53c588390c102761b` и SHA-256 `c04fa0642d928aec1535e90d78be3ea969582537c7eb082423bf50149cf0412e` по 57 exact `path → git blob SHA` records. Guard не импортирует и не запускает внешний runtime.
- Первый вариант manifest был намеренно не смержен после независимой проверки: 22/36 первоначальных evidence paths оказались мёртвыми на pinned SHA. Финальный contract заменил их реальными frozen blob references и отдельно защищает snapshot от одновременной подмены path/object SHA/digest.
- Отдельный Capability Parity workflow green, и тот же validator встроен в canonical `regression_gate.STEPS`; `ci/regression-contour` success. Coverage ratchets сохранены: `82.23%` combined / `73.99%` branch при тех же locked baselines.
- Единственный доказанный `missing` gap — `platform.account_consolidation`: ClientPlatform уже имеет atomic cross-channel identity linking и fail-closed conflict handling, но пока не имеет audited operator dry-run/apply consolidation workflow. Отдельный speech/provider experiment gap не был подтверждён pinned evidence и поэтому не был выдуман.
- M6-006 не меняет business/domain runtime behavior; production deploy намеренно не выполнялся. Issue #263 остаётся открытым до закрытия последнего `missing` либо явного owner exception.

### M6-007 — `DONE` — Audited Duplicate Account Consolidation

Последний proven capability-parity gap закрыт внутри существующего account/identity authority без второго identity store/runtime. Platform operator получил read-only dry-run и exact-plan-bound apply; stale/concurrent/conflicting состояния fail-close, а merged source сохраняет детерминированный redirect в canonical target.

### Evidence

- PR #282 (`M6-007: audited duplicate account consolidation`) squash-merged в `main` как `59029646142f96e217778c7a803464766390082e`; exact final PR head `6e269af891f0c3ec8375d83e0671dff85de83552`.
- Все 17 PR workflow на exact final head завершились `success`. `CI` полностью green: regression contour, static security, PostgreSQL payment/concurrency и coverage ratchets; отдельный `Postgres account consolidation concurrency` step также `success`.
- GitHub commit statuses green: `ci/regression-contour`, `AI Review / gate`, combined coverage `82.38% / 82.38%` и branch coverage `74.02% / 74.02%`. Coverage baselines усилены относительно входного `main` (`82.23% / 73.99%`), а environment-dependent logging coverage заменён детерминированным regression contract.
- Capability parity manifest теперь содержит 17 families / 20 capabilities / 20 proven / 0 missing; `platform.account_consolidation` переведён в proven `genericized` и включён в hard proven-capability ratchet.
- Consolidation использует canonical `accounts` + `account_channel_identities`: dry-run dependency inventory, exact SHA-256 plan fingerprint, explicit operator/reason/confirmation, durable idempotency, source/target serialization, stale-plan rejection, append-only operation/audit evidence и deterministic merged alias resolution.
- Identity collisions, same-business membership overlap, unsafe RBAC expansion, active OAuth, locked jobs, sending outbox и неизвестные identity dependencies блокируют apply до mutation. Historical audit/outcome provenance не переписывается; operational references переносятся только по доказанным policy.
- Messenger/tenancy continuity сохраняется через canonical alias resolution без synthetic membership/TenantContext. SQLite compatibility допускает только действительно отсутствующий account authority в isolated test fixtures; incompatible schema и production PostgreSQL остаются fail-closed.
- Локальный final-head evidence: full coverage suite `3192 passed, 7 skipped`, `REGRESSION_GATE_OK`, `COVERAGE_RATCHET_OK combined=82.38%/82.38% branch=74.02%/74.02%`, Canon/Product Purity/Capability Parity/Ruff/diff-check green.
- Issue #263 автоматически закрыт merge-событием #282 после того, как manifest уже имел `missing=0`; это соответствует acceptance condition parity contract.
- Production deploy не входил в code PR; владелец отдельно разрешил exact-main production rollout после merge и roadmap closure.

### M6-008 — `DONE` — Repository Merge Governance Enforcement

Закрыто 2026-09-03. GitHub `main` теперь защищён для всех, включая administrator path: PR обязателен, strict required checks включены, force-push/deletion запрещены, unresolved review conversations блокируют merge, постоянного bypass нет.

Доказательство закрытия:

- branch API после настройки: `protected=true`, enforcement=`everyone`, `strict=true`;
- 11 stable required contexts привязаны к GitHub Actions app: regression, combined/branch coverage, quality, PostgreSQL CI, static security, Canon, Brand/Product Purity, Capability Parity, Production Isolation и Pre-deploy Release Gate;
- safe negative probe: direct push probe commit `6ada870bb59dde57dd5436c3331a94a6952dcbe1` отклонён GitHub `GH006` с требованием PR и 11/11 checks; remote `main` не изменился;
- live force/delete probe намеренно не выполнялся как потенциально разрушительный; API фиксирует `allow_force_pushes=false`, `allow_deletions=false`;
- PR #285 стал positive non-deadlock proof: exact head `e3aa3d0f87261d4436cc824d31da8e3933a1e5d4`, 17/17 workflows success, required checks green, unresolved review threads=0, squash-merge `3f768752a75fef95b990d17e08f70db59cddf021` прошёл при активной protection;
- policy зафиксирована в `docs/REPOSITORY_MERGE_GOVERNANCE.md`; GitHub protection остаётся enforcement authority, документ не создаёт второй release/deploy authority;
- production runtime/data этим governance slice не менялись.

Минимальный DONE contract M6-008:

- `main` защищён GitHub branch protection/ruleset: direct push, force-push и deletion запрещены обычному пути; изменение production-кода попадает в `main` через PR;
- required checks закрепляют стабильные canonical gates как минимум для CI regression/security/coverage, Canon, Product Purity/Brand, Capability Parity, Production Isolation и Pre-deploy Release Gate; required contexts не должны зависеть от ephemeral run id;
- merge разрешён только для актуального exact PR head; stale head/base или красный required check блокируют merge;
- административный/break-glass bypass, если он вообще нужен, минимален, явно документирован и не превращается в постоянный обход governance;
- GitHub App/owner workflow сохраняет возможность штатно создавать и мержить green PR; правила не создают deadlock, в котором required status невозможно опубликовать;
- repository-level proof включает API evidence `protected=true` и активный ruleset/protection, negative probe для запрещённого direct/force path там, где это безопасно, и green PR merge proof без ослабления CI;
- никакой второй release/deploy authority не создаётся: production по-прежнему разворачивается только canonical exact-SHA deploy path после green merge;
- production runtime/data не меняются этим governance slice.


### M7-001 — `DONE` — Authenticated Business Cockpit Shell + Server-Authorized Navigation

Первый slice M7 создаёт не второй интерфейсный продукт, а безопасную оболочку над уже существующими canonical application/domain capabilities. Telegram bot остаётся быстрым action surface; Mini App становится понятным business cockpit.

Минимальный DONE contract M7-001:

- Telegram Mini App `initData` проверяется backend по официальной подписи и freshness; непроверенные frontend identity/business параметры не дают доступа;
- current business/workspace scope и RBAC вычисляются сервером из canonical membership/account authority; frontend `business_id`, role и route никогда не являются авторизацией;
- mobile-first cockpit shell содержит понятную навигацию к `Home / Today`, Customers, Calendar, Sales, Growth, Content, Automation, Analytics, Connections, Team, Billing, Settings / Privacy, но не дублирует их domain logic;
- недоступные разделы либо не предлагаются роли, либо дают ясное объяснение; backend deny остаётся обязательным даже при скрытой кнопке;
- существующие owner/help/onboarding формулировки используются как единая UX-система: у основных действий есть человеческое «что это» и «когда сюда нажимать» без уменьшения скрытого функционала;
- Telegram bot и Mini App используют одни application services/use-cases; не создаётся второй CRM, customer timeline, automation brain, billing authority или frontend-owned data store;
- navigation/deep-link state не позволяет tenant switching через URL/query/local storage; sensitive actions продолжают использовать canonical approval/consent boundaries;
- frontend не получает provider secrets, bot tokens или raw infrastructure credentials;
- regressions покрывают invalid/expired initData, forged business/role, cross-tenant route, role navigation, deep links, mobile shell, backend deny и отсутствие duplicate business logic;
- production deploy не является частью code slice без отдельной команды владельца.

Закрыто 2026-09-03. Доказательство M7-001:

- PR #287 squash-merged через защищённый `main`; final exact PR head `2be2d78c5e88fa6526bd45a51896781603af78d3`, merge `7e01f20ba1c57bd1f4475378eb68727e70fc56b9`; protection/bypass не ослаблялись;
- final head прошёл 17/17 PR workflows; required regression/security/PostgreSQL/Canon/Brand/Capability Parity/Production Isolation/Pre-deploy checks green; unresolved review threads=0; coverage ratchet `82.42%` combined / `74.07%` branch;
- Telegram Mini App admission использует raw signed `initData`, bot-token HMAC, constant-time compare, freshness/future-skew и canonical account alias resolution до tenancy; frontend identity/business/role не являются authority;
- business scope/RBAC и role-aware navigation остаются в существующих account/tenancy owners; cockpit не создаёт второй CRM, billing/automation brain, API process или durable frontend state;
- первоначальный Canon finding на top-level `pytest` в dependency-light `test_clientplatform_*` закрыт переводом security/HTTP regressions на canonical `unittest`, без исключения тестов из Canon;
- первоначальный Release Gate finding на wide tuple-except в verifier закрыт семантически узкой обработкой parse/decode ошибок; validator не обходился;
- при pre-production аудите найден и закрыт реальный proxy gap: broad `/clientplatform/*` media matcher перехватывал бы Mini App; explicit cockpit matcher добавлен перед media route и закреплён regression + real Caddy validation;
- exact-main production rollout выполнен canonical locked updater с encrypted backup и `full_runtime`; evidence `/var/lib/clientplatform/deploy-evidence/deploy-20260903T201326Z.json`, `ok=true`, `target_sha=7e01f20ba1c57bd1f4475378eb68727e70fc56b9`, baseline ready, 20s stability green, app/Caddy restart count=0;
- live HTTPS acceptance: `/clientplatform/cockpit`=200 с CSP/no-store, `/clientplatform/cockpit/app.js`=200 без `initDataUnsafe`/`localStorage`, forged `POST /clientplatform/cockpit/context` fail-closed=401 `invalid_init_data`; production dashboard строит единственную `🏠 Открыть кабинет` WebApp-кнопку на `https://app.clientplatform.ru/clientplatform/cockpit`; свежие app/Caddy severe-log scans чистые.

### M7-002 — `DONE` — Home / Today Cockpit Projection

Первый содержательный экран cockpit должен отвечать на вопрос владельца или сотрудника: «Что происходит сегодня и что мне делать дальше?». Он не создаёт новую task/analytics модель, а собирает разрешённую конкретной роли read-only проекцию из уже существующих canonical owners.

Минимальный DONE contract M7-002:

- каждый Home/Today запрос заново проходит M7-001 verified Telegram identity → canonical account alias → live tenant/RBAC; `business_id`/role/deep-link из frontend не дают дополнительных прав;
- transport-neutral application projection переиспользует существующие canonical read models: owner operating/action queue из `growth_cockpit`, sales/handoff work, bookings, customer activity, outcomes/unit economics и automation approvals только там, где текущая роль уже имеет право их читать; отдельного home-store/materialized «второго мозга» нет;
- экран показывает компактно «сегодня», «требует внимания» и «следующий шаг»; deterministic ordering/priority берётся у существующих owners, а не вычисляется новым скрытым score в frontend или HTTP adapter;
- карточки permission-aware: недоступные роли не получают raw customer/money/advertising facts; partial availability отображается понятным объяснением, а backend deny остаётся обязательным;
- today boundaries считаются в canonical business timezone; деньги остаются currency-safe и не суммируются через разные/неподтверждённые валюты;
- page load/read-only refresh не создаёт side effects, approvals, spend, outbound messages или durable tasks; mutation CTA только маршрутизирует в уже существующий canonical use-case/approval boundary;
- Home/Today API возвращает versioned/bounded payload без provider secrets, raw credentials и инфраструктурных деталей; ошибки отдельного optional source не раскрывают чужие данные и не превращают unavailable signal в ноль/успех;
- mobile UI объясняет человеческим языком «что произошло», «почему это важно» и «что нажать», сохраняя быстрый Telegram bot action surface;
- regressions покрывают owner/admin/manager/support/content/marketer/analyst visibility, cross-tenant forged business, stale/revoked membership, business-local day boundary, empty day, partial-source failure, deterministic action order, money/currency safety, no mutation on GET/refresh и deep-link authorization;
- production deploy выполняется только после green protected merge и отдельной команды владельца.

Закрыто 2026-09-04. Доказательство M7-002:

- PR #290 final exact head `6a76a36b9e72fbd218efb3f7f61a01b1066cb45d` прошёл 17/17 pull-request workflows и squash-merged через защищённый `main` как `0b426312541dc5f86e3ef01edc4f5dc74476807b`; protection/bypass не ослаблялись, unresolved review threads=0;
- combined coverage `82.42%` и branch coverage `74.07%` удержали существующий ratchet без снижения baseline; Canon, Product Purity, Capability Parity 20/20, Production Isolation, Release Gate, PostgreSQL concurrency, critical pinned mypy/Bandit и User Scenario Matrix green;
- `Home / Today` — versioned bounded read-only projection без отдельного home-store: он композиционно использует существующие growth/action queue, sales/handoff, booking, customer activity, outcome/money и automation-approval owners;
- customer handoff/sales priority не скопирована: существующая deterministic composition из `growth_cockpit` выделена в один transport-neutral helper и продолжает владеть порядком для старого и нового surface;
- каждый refresh повторяет signed Telegram initData admission, canonical account alias, business selection и live membership/RBAC; `TenantAccessDenied` никогда не превращается в optional-source warning;
- role matrix доказана regressions: owner/admin/manager получают разрешённые customer/outcome/money сигналы; support — customer/sales/booking без money; marketer — только разрешённые локальные automation signals; content/analyst не получают customer/money leakage;
- today boundary вычисляется в canonical business timezone; multi-currency money остаётся раздельным по ISO currency, неизвестная валюта/повреждённый booking source становятся explicit unavailable, а не ложным нулём;
- Home refresh не вызывает Yandex/provider analytics: provider read path может обновлять credential bundle при refresh токена, поэтому он остаётся только явным Growth surface, а ежедневное открытие Home не имеет скрытых network/credential side effects;
- UI использует `textContent`, не хранит tenant/role в URL/localStorage, Home CTA только маршрутизирует к уже server-authorized разделу и не выполняет approval/send/spend; cockpit runtime включён в централизованный critical type/security manifest;
- production deploy M7-002 не выполнялся: текущая команда владельца была на продолжение разработки, а roadmap требует отдельного explicit production command для каждого code slice.

### M7-003 — `DONE` — Customers & CRM Cockpit

Следующий содержательный экран превращает уже существующую canonical customer/CRM модель в понятный мобильный рабочий surface. Он не создаёт вторую CRM, customer table, timeline store или sales state machine: Mini App только ищет, отображает и маршрутизирует разрешённые действия к существующим owners.

Минимальный DONE contract M7-003:

- каждый list/search/detail/timeline request повторяет M7-001 signed identity → canonical account alias → selected business → live tenant/RBAC; customer UUID из frontend никогда не является authority и cross-tenant/stale membership fail-close;
- transport-neutral customer projection переиспользует canonical `customers`, `customer_timeline`, sales work/handoff и customer identity owners; никакой второй durable customer index/timeline store не создаётся;
- список bounded/paginated и безопасно ищется по разрешённым display/identity-derived полям без передачи raw external provider subject/token/credential; поиск не допускает unbounded full-table payload;
- карточка клиента показывает понятные основные сведения, следующий customer/sales step и последние timeline events; money/attribution fragments появляются только там, где текущая роль уже имеет canonical ledger permission;
- support/owner/admin/manager visibility сохраняет существующий customer-record permission, а marketer/content/analyst не получают customer PII через скрытые endpoints/deep links; frontend hide не заменяет backend deny;
- timeline остаётся read-only canonical chronology из существующих facts; порядок/дедупликация принадлежат `get_customer_timeline`, а Mini App не материализует и не переоценивает события;
- действия из карточки — только deep-link/route к существующим customer/sales/booking use-cases; создание/архивирование/сообщение/оплата/продажа не выполняются GET/list/detail refresh и требуют своих текущих permission/idempotency/approval boundaries;
- API payload versioned/bounded, no-store, без secrets/raw infrastructure metadata; malformed optional source отображается как unavailable без утечки чужого tenant;
- mobile UX позволяет дилетанту: найти клиента → понять последнюю историю → увидеть, что делать дальше → перейти в нужное действие, с человеческими «что это»/«когда нажимать»;
- regressions покрывают owner/admin/manager/support и denied marketer/content/analyst, search pagination/bounds, forged customer/business, revoked membership, timeline role redaction, deterministic order, no raw external identity leakage, no mutation on refresh и authorized action routing;
- production deploy только после green protected merge и отдельной explicit команды владельца.

Закрыто 2026-09-05. Доказательство M7-003:

- PR #295 final exact head `eeb0136e749650efcb1a6f3dc785d3352b5f946a` прошёл 17/17 pull-request workflows и squash-merged через защищённый `main` как `f6e6a0550853b044b48f285ee9561f7c8351d2d8`; unresolved review threads=0, protection/bypass не ослаблялись;
- full coverage run: `3251 passed, 8 skipped`; combined coverage поднят и зафиксирован ratchet с `82.42%` до `82.46%`, branch coverage с `74.08%` до `74.13%`; regression contour, Critical Static, Release Gate, Production Isolation, Capability Parity 20/20 и PostgreSQL/concurrency green;
- customer list/search/detail bounded и tenant-scoped; signed Telegram initData, canonical account alias, selected business и live membership/RBAC повторно проверяются server-side, а marketer/content/analyst не получают customer PII;
- timeline остаётся canonical `get_customer_timeline`; customer next-step берётся из существующих sales work/handoff owners, raw provider identity/credential metadata не становится frontend authority;
- действие из карточки re-readится на click, stale action fail-close, затем компактный first-party Telegram deep-link маршрутизирует в существующие handoff/work/lead presentation owners; refresh/list/detail не выполняют sales mutation и не создают второй CRM/sales brain;
- Independent AI Review policy gate завершился `success` с явным trusted-policy verdict `L2 external AI review temporarily disabled by trusted repository policy`; внешний L2/Codex review фактически не выполнялся из-за исчерпанной review quota, что не скрывается в evidence;
- production deploy M7-003 не выполнялся: он остаётся отдельной explicit owner-командой.

### UX-294 — `DONE` — Safe retirement of outdated offers and business profile

Источник: open owner issue #294. Это lifecycle/UX slice, а не новый store или второй business owner.

Минимальный DONE contract UX-294:

- владелец может убрать устаревший offer/service/post из активных пользовательских поверхностей только через явное действие с confirmation;
- финансовые, outcome, audit и attribution facts не уничтожаются каскадно: retirement/deactivation отделяется от immutable/history-bearing records;
- удаление/отключение business profile/type не создаёт второй business lifecycle owner и использует canonical tenancy/business membership boundaries;
- после подтверждённого удаления/retirement старый business не остаётся ложным активным выбором в Telegram/WebApp navigation, а владелец может создать/подключить другой business через существующий onboarding;
- tenant isolation и RBAC проверяются backend-side, forged business/object IDs fail-close; повторное действие идемпотентно и не оставляет частично удалённое состояние;
- regressions покрывают confirmation/cancel, stale/repeated request, cross-tenant access, preserved financial/audit history, navigation cleanup и clean owner transition;
- production deploy только после green protected merge и отдельной explicit команды владельца.

Закрыто 2026-09-05. Доказательство UX-294:

- PR #297 final exact head `6cbe13b4870f24d6a75dc722a178311ea0df99df` на GitHub завершил 28/28 exact-head workflow runs с `success` и squash-merged через защищённый `main` как `03590594208cee89009e5a06855808e27d975c6a`; issue #294 закрыт merge, unresolved review threads=0, protection/bypass не ослаблялись;
- full coverage run: `3257 passed, 8 skipped`; combined coverage сохранён на `82.46%`, branch coverage реально вырос с `74.13%` до `74.15%` и новый ratchet зафиксирован без снижения порогов; `REGRESSION_GATE_OK`, Critical Static (`CRITICAL_MYPY_OK`, `CRITICAL_BANDIT_OK`), Pre-deploy Release Gate, Production Isolation, Capability Parity и PostgreSQL payment/concurrency green;
- offer/service retirement использует существующий activity owner и переводит canonical offering в `archived`; publication retirement остаётся в canonical `business_publications`; business retirement выполняется существующим tenancy owner, а immutable financial/outcome/audit history физически не удаляется;
- stale publication schedule/publish callbacks и stale direct booking claims после retirement fail-close; active invites отзываются без удаления evidence, transient workspace/input/onboarding pointers очищаются, поэтому архивированный business не остаётся ложным активным выбором и владелец может пройти существующий onboarding для другого business;
- Codex code review фактически не выполнялся из-за исчерпанной review quota; бот оставил только служебное сообщение о лимите, поэтому это **не** записывается как AI-review pass; код прошёл обязательные protected checks и ручной architecture/diff review без unresolved threads;
- production deploy UX-294 не выполнялся: он остаётся отдельной explicit owner-командой; в roadmap нет `QUEUED` successor, поэтому новый `NEXT` не придумывается без отдельного owner/canonical решения.

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
| M5-001 Canonical AutomationPolicy Foundation | DONE | PR #245 squash-merge `0c813605c23e1d8e6f1f5d4c7f85193a9b09a209`; exact head `536d8e35429c8695f110c09f345fd67303c39d85`; all 16 PR workflows success; 2 P1 review findings resolved; focused 14 tests + full coverage regression `4151 passed, 7 skipped`; coverage 75.00% combined / 66.26% branch; no autonomous execution and no production deploy |
| M5-002 Canonical Action Approval Boundary | DONE | PR #247 squash-merge `1cbee98d85131c0e6579e292e8413a5ae71b7613`; exact head `6b985a35d73244bebcde56edcc4905efc19e2398`; all final-head PR checks green; 2 review findings resolved; focused 33 tests + full regression `4172 passed, 7 skipped`; coverage locked at 75.02% combined / 66.29% branch; no provider execution or production deploy |
| M6-001 Platform Operator Read-Only Snapshot | DONE | PR #264 merge `6acdcb62f6b644592aa23301ad1763a747b3b712`; exact head `c2f32fa2914245c732a8bc06d32d334bcb1454fd`; all final-head PR checks green; review P1 resolved with hidden `/platformstatus` + deny/allow surface regressions; full CI `3070 passed, 7 skipped`; coverage locked at 81.99% / 73.64%; exact-SHA production deploy `deploy-20260902T151537Z.json` with encrypted backup, health/readiness, HTTPS/polling-only contract, restart=0 and 20s stability |
| M6-002 Audited Support Access Session | DONE | PR #266 merge `ddc0bd67ad9e7be549e6c5a840af36f8e5f2a402`; exact head `2e56a1498526b76526a8516cdf5badba3e88f1ae`; all 16 workflows success; GitHub coverage `82.08%` within `82.09%` baseline tolerance and branch `73.73% / 73.73%`; local full suite `3088 passed, 7 skipped`; isolated PostgreSQL 16 replay/read/revoke smoke green; no membership injection or synthetic TenantContext; production deploy not part of slice |
| M6-003 Support Case Intake + Operator Queue | DONE | PR #268 merge `5cc038e7b2a6a617e2a07ecfb223d580f4e48ec0`; exact head `07504bf975fcc23ecdf65c793aa9b040d648dc7f`; all 16 workflows + AI Review green; review P1s resolved; coverage locked at 82.19% / 73.93%; exact-SHA production deploy `deploy-20260902T200829Z.json` with encrypted backup, health/readiness, restart=0 and 20s stability |
| M6-004 Bounded Platform Directory Search + Access Review | DONE | PR #273 merge `6b21f928272e97db5b8b79bb3adc37db62bf5798`; exact head `33f9679907be60c26f7474bcf709367bef456f84`; all 16 workflows green; 2 review findings resolved; full CI `3138 passed, 7 skipped`; coverage `82.23% / 73.99%`; exact-SHA production deploy `deploy-20260903T052535Z.json` with encrypted backup, health/readiness, restart=0 and 20s stability |
| M6-005 Disk-safe Production Deploy Retention | DONE | PRs #275/#276/#277/#278; final merge `48ee169cc72e687d0d8d2e89fbbe748eca41fd8b`; full-runtime 75%/7GiB unchanged; proven host-only no-build deploy accepted in production with encrypted backup, unchanged container IDs/restart=0 and 20s stability |
| M6-006 Versioned Capability Parity Matrix + Regression Guard | DONE | PR #280 squash-merge `8ffa7699c399aa613e64fffcba2448182e0542fe`; exact head `2ef607e976046a1833d7bf31fa58714776c66abb`; 17/17 workflows + Independent AI Review green; executable matrix = 17 families / 20 capabilities / 19 proven hard-ratcheted / 1 explicit missing; frozen pinned evidence = 57 exact blobs; canonical regression + coverage `82.23% / 73.99%` green; no production deploy |
| M6-007 Audited Duplicate Account Consolidation | DONE | PR #282 squash-merge `59029646142f96e217778c7a803464766390082e`; exact head `6e269af891f0c3ec8375d83e0671dff85de83552`; 17/17 workflows green; PostgreSQL consolidation concurrency green; parity = 20/20 proven, missing=0; coverage locked at 82.38% / 74.02%; #263 closed at merge |
| M6-008 Repository Merge Governance Enforcement | DONE | GitHub `main` protected for everyone; strict 11-context required-check set; direct push negative probe rejected with `GH006`; force/delete disabled; PR #285 exact head `e3aa3d0f87261d4436cc824d31da8e3933a1e5d4` passed 17/17 workflows and merged through active protection as `3f768752a75fef95b990d17e08f70db59cddf021`; no production runtime/data change |
| M7-001 Authenticated Business Cockpit Shell + Server-Authorized Navigation | DONE | PR #287 final head `2be2d78c5e88fa6526bd45a51896781603af78d3`, squash-merge `7e01f20ba1c57bd1f4475378eb68727e70fc56b9`; 17/17 workflows green; coverage `82.42% / 74.07%`; exact-SHA full-runtime production deploy `deploy-20260903T201326Z.json` with encrypted backup, restart=0, 20s stability and live cockpit HTTPS/auth acceptance |
| M7-002 Home / Today Cockpit Projection | DONE | PR #290 exact head `6a76a36b9e72fbd218efb3f7f61a01b1066cb45d`, squash-merge `0b426312541dc5f86e3ef01edc4f5dc74476807b`; 17/17 workflows green; coverage `82.42% / 74.07%`; role/timezone/currency/no-side-effect Home contracts proven; no production deploy |
| M7-003 Customers & CRM Cockpit | DONE | PR #295 final exact head `eeb0136e749650efcb1a6f3dc785d3352b5f946a`, squash-merge `f6e6a0550853b044b48f285ee9561f7c8351d2d8`; 17/17 workflows green; full coverage `3251 passed, 8 skipped`; ratchet raised to `82.46% / 74.13%`; canonical customer/timeline/sales ownership, live RBAC, PII redaction, stale-action fail-close and click-only action routing proven; AI policy gate success under trusted temporary L2-disable policy; no production deploy |
| UX-294 Safe retirement of outdated offers and business profile | DONE | PR #297 final exact head `6cbe13b4870f24d6a75dc722a178311ea0df99df`, squash-merge `03590594208cee89009e5a06855808e27d975c6a`; 28/28 exact-head workflow runs success; full coverage `3257 passed, 8 skipped`; ratchet `82.46% / 74.15%`; safe canonical offering/publication/business retirement, preserved financial/outcome/audit history, stale-action fail-close, tenant/RBAC isolation and clean owner transition proven; #294 closed; no production deploy |

---

# 28. Финальная продуктовая цель

ClientPlatform считается пришедшим к целевой форме не тогда, когда в нём будет много интеграций, а когда типичный малый бизнес сможет сказать:

> «Я подключил ClientPlatform, рассказал, чем занимаюсь, и дальше он реально помогает мне находить клиентов, не терять их, продавать, возвращать, выдавать материалы/услуги, считать деньги и сам делать безопасную рутину. Я вижу, что он сделал, сколько это принесло и могу в любой момент изменить пределы или остановить его.»

Техническая сложность должна расти **внутри** платформы. Внешний пользовательский сценарий должен становиться проще, короче и результативнее.

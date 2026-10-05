# AI-конструктор продающего лендинга мероприятия

Дата: 2026-10-05  
PR: #574

## Канонический путь

Существующий публичный event endpoint `/e/<slug>` остаётся единственной точкой входа для регистрации на мероприятие.

Новый продающий слой не создаёт второй CRM, вторую форму регистрации, отдельную атрибуцию или отдельный event runtime. Он только меняет presentation-слой над уже существующей формой.

Каноническая цепочка остаётся:

`реклама / публикация -> /e/<slug> -> /e/<slug>/register -> CRM -> reminders -> messenger links -> event -> follow-up -> analytics`.

`source` и `campaign_ref` проходят через прежнюю форму без изменения контракта.

## Состояния лендинга

Для каждого event может существовать один tenant-scoped landing profile:

- draft;
- published snapshot;
- revision;
- published revision;
- short-lived preview capability;
- durable AI egress claim.

Открытие редактора не пишет в БД. Если durable draft ещё отсутствует, UI строит безопасную deterministic template-проекцию из фактов event и подтверждённых Business Profile facts.

AI и ручные правки изменяют только draft. Публичная страница меняется только после явного `Опубликовать`.

`Вернуть простой лендинг` отключает published selling layer, не удаляя event, registrations или funnel history.

## Блоки

Selling landing поддерживает:

- hero / offer;
- аудиторию;
- ожидаемую пользу без гарантий;
- программу;
- организатора;
- FAQ;
- финальный CTA;
- темы оформления calm / bold / minimal.

HTML всегда экранируется. Join URL и provider credentials в landing copy не передаются.

## AI-контур

Конструктор использует существующий canonical `services.ai` provider router. Он не создаёт отдельный provider path и может работать через выбранный в ClientPlatform Yandex / DeepSeek / GigaChat / OpenAI-compatible provider.

Перед внешним AI egress есть отдельное подтверждение владельца.

В AI передаются только:

- event kind/title/description/schedule;
- business name и activity description;
- подтверждённые маркетинговые факты: services, products, prices, audiences, geo, tone, allowed/prohibited claims, legal constraints, FAQ, sales terms, preferred conversion action;
- текущая safe template.

Не передаются registration records, customer identities, participant contacts, Business Profile contacts, source URLs или visual asset references.

AI не имеет права самостоятельно публиковать результат.

## Защита платного AI-вызова

До egress создаётся durable confirmation receipt, привязанный к event + конкретной draft revision + normalized input digest. Подтверждающая кнопка несёт именно эту revision; provider egress разрешён только переходом `confirming -> planning` для того же receipt.

Повторный callback:

- не запускает второй provider call, если первый ещё `planning`;
- после успешного `ready` старое подтверждение является терминальным и не может переиспользовать новую draft revision;
- если процесс исчез после возможной отправки запроса и `planning` протухает, состояние переводится в `ambiguous`, а не перезапускается;
- `ambiguous` сохраняется даже при обычном редактировании draft;
- новая AI-попытка после `ambiguous` возможна только после отдельного owner action «Разобраться с AI-вызовом» → явного подтверждения разблокировки риска повторного списания; сама разблокировка AI не запускает.

Если владелец меняет draft во время AI-вызова, stale AI result не может перезаписать новую ревизию.

## Preview

Preview token:

- high-entropy;
- в БД хранится только SHA-256 digest;
- ограничен TTL;
- привязан к конкретной draft revision;
- инвалидируется при изменении draft/publish;
- отдаётся с `noindex, nofollow`;
- не отправляет реальную registration form.

## Rollout и recovery

Selling layer является optional presentation layer.

Если новая landing-table ещё отсутствует в момент rolling update, публичный `/e/<slug>` продолжает отдавать прежний простой лендинг. Только ошибка missing landing schema допускает этот fallback; другие DB failures не маскируются.

Повреждённый published landing JSON также не должен уронить canonical registration page — система возвращается к простому presentation layer.

## Омниканальность

Owner builder доступен через тот же application/domain contract в:

- Telegram;
- VK;
- MAX.

В VK/MAX конструктор открывается из контент-плана конкретного вебинара, чтобы не раздувать общий webinar hub и не нарушать native button limits.

## Основные регрессии

`tests/test_clientplatform_event_landing_builder.py` покрывает:

- строгую domain schema;
- draft != published;
- preview TTL/revision;
- tenant isolation и live membership re-authorization;
- CAS и stale writes;
- durable AI confirmation/revision binding;
- terminal successful AI receipt and stale-button replay;
- abandoned `planning -> ambiguous` recovery;
- persistent ambiguous lock + explicit owner resolution;
- stale AI completion;
- data minimization;
- optional-schema rollout;
- corrupt optional landing fallback;
- registration/attribution contract;
- schema upgrade idempotency.

`tests/test_clientplatform_webinar_uiux_parity.py` и `tests/test_handlers_clientplatform_events.py` покрывают native Telegram/VK/MAX navigation и callback safety.

# ADR-0126: owner feedback and measurable value proof for external observations

**Статус:** принято реализацией vertical slice

## Контекст

ADR-0124 добавил provider-neutral external observations в существующий Customer Timeline,
а ADR-0125 добавил exact revision, freshness/unknown, retraction и tenant off-switch.
UIII Canon требует до влияния нового сигнала на business policy сначала показать его
пользователю, получить feedback и измерить фактическую прибавку. Зелёный CI сам по себе
не доказывает полезность источника.

ClientPlatform остаётся владельцем Customer, CustomerIdentity, Sales, Outcome,
AutomationPolicy, consent/suppression и всех бизнес-действий.

## Решение

1. Для текущей head-revision structured observation владелец/роль с правом изменения
   Customer может оставить одну из трёх оценок: `useful`, `incorrect`,
   `wrong_customer`.
2. Feedback хранится отдельно от immutable source receipt. Он является оценкой
   потребителя, а не новым доказательством внешнего факта.
3. Один receipt имеет одну текущую оценку; повторная оценка обновляет её идемпотентно.
4. Оценивать можно только current head observation. Старая superseded revision
   fail-closed и не принимает новый feedback.
5. `wrong_customer` является диагностическим сигналом. Он **не** переносит identity,
   не разрывает binding, не объединяет Customer и не архивирует запись автоматически.
6. Feedback не создаёт Outcome, Sales stage, next action, follow-up, send, approval и
   не меняет AutomationPolicy/consent.
7. Cockpit показывает сохранённую оценку рядом с observation и даёт owner-facing
   кнопки «Полезно», «Неверно», «Не тот клиент». Роли только для чтения видят факт,
   но не получают mutation control.
8. Tenant-scoped value snapshot считает только current heads:
   active/retracted, stale active, unknown freshness и feedback totals по трём классам.
   Это baseline для проверки useful-source value; проценты/рейтинги не выдумываются.
9. Feedback table относится к customer-linked tenant data и удаляется по privacy
   policy вместе с бизнесом/receipt.
10. Этот slice по-прежнему не даёт external observations права влиять на decision
    contour. Такое влияние допустимо только после реального source sample, измеренной
    полезности и отдельного review существующей policy boundary.

## Пользовательский результат

В карточке Customer рядом с внешним observation владелец может отметить:

- **Полезно** — сигнал действительно добавил контекст;
- **Неверно** — источник/наблюдение ошибочно;
- **Не тот клиент** — связь выглядит ошибочной и требует разбора.

Оценка сохраняется и видна после перезагрузки. Она не выполняет скрытых действий.

## Метрика полезности

`ExternalObservationValueSnapshot` предоставляет абсолютные счётчики:

- current observations;
- active/retracted;
- stale active;
- unknown freshness;
- feedback total;
- useful;
- incorrect;
- wrong customer.

До появления реального источника это техническая готовность измерения, а не доказанный
uplift. После пилота эти счётчики должны сопоставляться с заранее заданными условиями
продолжения источника.

## Не входит в этот slice

- автоматический rebind/split по `wrong_customer`;
- влияние observation/feedback на Sales AI, Next Best Action или отправку;
- live credentials конкретного UIII/provider;
- causal uplift claim без контрольного периода;
- отдельный UIII CRM/EvidenceStore внутри ClientPlatform.

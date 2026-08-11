# ClientPlatform

ClientPlatform — мультитенантная платформа и цифровой сотрудник для специалистов, самозанятых и малого бизнеса.

Главный продуктовый принцип:

> **Пользователь управляет результатами, а не технологиями.**

Владелец бизнеса не должен разбираться в API, токенах, webhook, JSON, очередях, OAuth, идентификаторах рекламных кабинетов или внутреннем устройстве провайдеров. В обычном сценарии он сообщает, чего хочет добиться, а ClientPlatform выполняет техническую работу внутри безопасных границ и показывает результат.

## Статус проекта

ClientPlatform развивается как отдельный продукт со своей предметной моделью, архитектурой, production-контуром и каноном.

Исторически репозиторий был создан из независимой копии технического baseline `metrotherapy-bot-telegram` на коммите:

```text
b4ac43c2961fb581078aedc25efeffd2ab4ecb34
```

Происхождение зафиксировано в [`docs/BASELINE_PROVENANCE.md`](docs/BASELINE_PROVENANCE.md), но оно **не определяет текущую предметную модель ClientPlatform**. Канонические продуктовые и архитектурные решения находятся в [`docs/CLIENTPLATFORM_CANON_TZ.md`](docs/CLIENTPLATFORM_CANON_TZ.md).

В репозитории ещё могут встречаться legacy-названия и миграционные артефакты. Они не являются нормативными: при любом противоречии приоритет имеет канон ClientPlatform.

Проект имеет отдельный production deployment, fail-closed preflight, автоматизированный rollback, encrypted backup, production-isolation проверки и расширенный CI. Это не означает, что весь долгосрочный roadmap уже завершён: отдельные каналы и продуктовые вертикали продолжают развиваться.

## Критическое ограничение

ClientPlatform **запрещено запускать с production-конфигурацией Метротерапии**. Нельзя использовать её Telegram-токены, PostgreSQL/backup-контур, платёжные credentials, webhook, домены, object storage, systemd units, серверные пути или реальные пользовательские данные.

Репозиторий остаётся публичным по решению владельца. Поэтому реальные секреты, `.env`, DSN, ключи, дампы и пользовательские данные запрещены в GitHub, Actions logs и artifacts.

## Что уже реализовано

### Мультитенантное ядро

- изолированные `Business` / `BusinessMember`;
- серверная проверка tenant scope;
- роли и RBAC;
- поддержка нескольких бизнесов у одного пользователя;
- отдельные customer- и owner-контуры;
- privacy/erasure contracts для данных бизнеса.

### Telegram как основной рабочий интерфейс

- центральный управляющий бот ClientPlatform;
- персональная админка специалиста;
- managed client bots;
- onboarding владельца и клиента;
- booking, услуги, клиенты и расписание;
- выдача материалов и программ;
- напоминания и операционные фоновые задачи.

### Goal-first UX

Основной интерфейс строится вокруг результата, а не вокруг технических разделов.

Для привлечения клиентов обычный путь начинается с понятного действия вроде:

```text
🚀 Получить клиентов
```

ClientPlatform может самостоятельно использовать безопасно известные настройки, выбрать подходящее свободное время и подготовить следующий шаг, не заставляя владельца выбирать внутренние идентификаторы и механизмы провайдеров.

При этом расширенный контроль сохраняется: владелец может изменить текст, использовать собственную картинку или видео, отказаться от медиа либо открыть дополнительные настройки.

### Реклама и Yandex Direct

В кодовой базе реализован защищённый контур Yandex Direct:

- OAuth и tenant-scoped рекламные подключения;
- выбор/повторное использование допустимых кампаний и регионов;
- подготовка рекламного DRAFT;
- синхронизация пользовательского текста;
- загрузка и привязка пользовательских изображений;
- загрузка, ожидание конвертации и привязка видео;
- опциональная генерация изображения через Visual Creative Engine;
- restart-safe обработка media jobs;
- удаление stale provider-side media при замене или отказе от медиа.

Платные действия отделены от подготовки рекламы. Расходы не разрешаются по одному факту существования кампании или черновика.

### Безопасность рекламных расходов

Запуск рекламных расходов использует отдельный fail-closed контур:

- свежая проверка состояния провайдера перед мутацией;
- точный ограниченный hard cap и daily cap;
- явное согласие владельца на конкретный денежный предел;
- повторное подтверждение, если предел изменился;
- immutable consent receipt;
- защита от duplicate tap / stale state;
- operator kill switch;
- concurrency-защита;
- автоматические stop-controls.

AI-генерация изображения также не должна расходовать платную квоту без отдельного явного действия владельца.

## Архитектурный принцип

ClientPlatform разделяет предметную логику, application orchestration, инфраструктуру и внешние интеграции. Внешний провайдер не должен становиться «вторым мозгом» продукта: правила бизнеса, tenant isolation, права, денежные ограничения и пользовательские сценарии остаются внутри ClientPlatform.

Ключевые каталоги:

```text
clientplatform/   domain, application, infrastructure, integrations
handlers/         Telegram presentation / interaction layer
services/         shared runtime services
core/             runtime primitives and common infrastructure
dashboard/        dashboard / Mini App related code
deploy/           deployment contracts and production assets
docs/             canon, ADR, architecture and operational documentation
scripts/          validation, maintenance and deployment tooling
tests/            regression and integration coverage
```

## Канон

Единственный нормативный документ продукта:

- [`docs/CLIENTPLATFORM_CANON_TZ.md`](docs/CLIENTPLATFORM_CANON_TZ.md)

Перед существенным изменением проекта необходимо сверяться с ним. Если код, README, старый отчёт или комментарий ему противоречит, приоритет имеет канон.

Проверка канона:

```bash
python scripts/check_clientplatform_canon.py
```

## CI и регрессионная защита

`main` должен оставаться рабочим. Репозиторий использует несколько независимых GitHub Actions-контуров, включая:

- основной regression contour;
- total coverage ratchet;
- branch coverage ratchet;
- critical static surface;
- canon / brand / boundary diagnostics;
- booking concurrency;
- ad-spend concurrency;
- partner dispatch concurrency;
- managed bot gateway / provisioning;
- production isolation;
- encrypted backup;
- user scenario matrix;
- pre-deploy release gate.

Актуальные минимальные coverage-пороги хранятся в [`coverage-baseline.json`](coverage-baseline.json). Порог должен повышаться при улучшении покрытия и не должен снижаться ради зелёного CI.

## Production

Production ClientPlatform — отдельный Docker Compose deployment в [`deploy/clientplatform`](deploy/clientplatform).

Операционный runbook:

- [`deploy/clientplatform/OPERATIONS.md`](deploy/clientplatform/OPERATIONS.md)

Основной безопасный updater:

```text
deploy/clientplatform/update-production.sh
```

Он поддерживает проверку ожидаемого SHA, блокировку параллельных deploy, preflight/deploy-контракт, post-deploy stability window и rollback при нестабильном runtime.

Production deploy выполняется только по прямому указанию владельца. Перед ним должны быть выполнены условия из runbook: чистый production checkout, корректный `clientplatform.env`, fail-closed preflight и обязательные backup/readiness contracts.

Нельзя:

- использовать production-секреты или данные Метротерапии;
- публиковать секреты, DSN, `.env`, дампы или пользовательские данные в GitHub/Actions;
- отключать tenant isolation, backup, polling contract или preflight ради deploy;
- обходить bounded consent для рекламных расходов;
- делать обычный deploy через `docker compose down` или удаление production volumes.

## Production topology

Канонический deployment использует:

```text
postgres  -> PostgreSQL 16
app       -> ClientPlatform runtime
caddy     -> public HTTPS reverse proxy
backup    -> encrypted backup profile
s3-replication -> optional off-site replication profile
```

Публично открываются только 80/443 через Caddy. Внутренние health/readiness endpoints не являются публичным API. Telegram production contract использует polling.

## Локальная разработка

Точные зависимости и команды определяются текущими конфигурационными файлами репозитория. Базовые проверки перед PR должны включать релевантные тесты, статический анализ и канонические validators.

Обязательный принцип работы с GitHub:

1. начинать от актуального `main`;
2. делать изменение в отдельной осмысленной ветке;
3. добавлять или обновлять регрессионные тесты для изменения поведения;
4. открывать PR;
5. не ослаблять тесты или validators ради зелёного результата;
6. не мержить красный CI;
7. production deploy выполнять отдельно и только после прямого указания владельца.

## Product roadmap

Цель ClientPlatform шире текущих production-вертикалей. Канон предусматривает развитие:

- Telegram Mini App;
- автопостинга и готовых воронок;
- дополнительных каналов, включая VK и MAX;
- аналитики и автоматизаций;
- программ, материалов, заявок и продаж;
- безопасного автопилота в заранее утверждённых владельцем пределах.

Roadmap не следует путать с уже активированным production-функционалом. Наличие интеграции в коде и её включение в конкретном production environment — разные состояния.

## Безопасность репозитория

Репозиторий остаётся публичным по решению владельца. Поэтому здесь запрещены:

- реальные Telegram/API/payment credentials;
- production `.env`;
- PostgreSQL DSN с секретами;
- encryption/private keys;
- backup dumps;
- персональные данные пользователей;
- production tokens и webhook secrets.

Секреты должны поступать только через предусмотренные production secret/environment contracts.

---

ClientPlatform строится не как набор кнопок вокруг интеграций, а как система, которая принимает намерение владельца, безопасно выполняет техническую работу и возвращает понятный бизнес-результат.

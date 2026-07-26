# Метротерапия (Aiogram 3)

Проект бота на **Aiogram 3**. Актуальная версия релиза определяется только файлом `VERSION`; README намеренно не дублирует номер версии.

## Production ingress contract

Канонический production-режим для Telegram: **polling only**.

- `TELEGRAM_TRANSPORT=polling`
- `TELEGRAM_WEBHOOK_ENABLED=0`
- `TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED=0`
- Telegram updates не принимаются через public webhook в production.
- Локальный aiohttp ingress может использоваться для MAX/VK, YooKassa reconciliation и media/audio links, но не должен становиться Telegram update ingress в production.
- Telegram webhook-код в репозитории остаётся compatibility/dev capability, а не production-contract.

## Production storage contract

Каноническое production-хранилище: **Postgres only**.

- `METRO_DB_ENGINE=postgres`
- `DATABASE_URL=postgresql://...`
- SQLite разрешён только для local/dev/hermetic tests.
- Production gate должен падать, если `APP_ENV=prod` запущен без Postgres.

## Production monetization contract

Канонический production-режим пакетной экономики: **hard token enforcement**.

- `TOKEN_ENFORCEMENT_MODE=hard`
- Soft/off режимы допустимы только для local/dev migration drills.
- Если у пользователя нет доступных практик, paid audio не должен выдаваться как оплаченный доступ.
- Цифровые пакеты внутри Telegram оплачиваются только нативными Telegram Stars (`XTR`).
- После выбора пакета пользователь сначала выбирает: Stars уже есть или Stars нужно купить. Инвойс создаётся только после этого выбора и принятия условий.
- `TELEGRAM_YOOKASSA_ENABLED=0` является обязательным production-контрактом. Старые Telegram→ЮKassa ссылки отклоняются; VK, MAX и web продолжают использовать ЮKassa независимо.
- Telegram Stars и YooKassa для внешних каналов используют единые идемпотентные сервисы начисления практик.
- Stars имеют фиксированную продуктовую лестницу: `1500 / 2500 / 5000 / 15000 XTR`; production запрещает вычисляемый `buyer_parity`.
- Checkout intent для внешних каналов фиксирует пользователя, платформу, пакет, сумму и валюту; изменение любого поля отклоняется до обращения к ЮKassa.
- Возврат Stars выполняется администратором в два шага: `/refundstars <charge_id>` и `/refundstars <charge_id> CONFIRM`. Уже использованный доступ автоматически не возвращается.

## Структура
- `main.py` — запуск
- `app.py` — сборка приложения (DP, роутеры, scheduler, init_db)
- `handlers/` — обработчики команд/колбэков
- `services/` — БД, схемы, логика доступа, scheduler, funnel
- `keyboards/` — клавиатуры
- `audio/` — контент
  - `audio/demo/` — демо-файлы (work/home)
  - `audio/full/` — полный доступ (если используется)
- `data/` — runtime data для local/dev; production state должен жить вне repo или в Postgres

## Установка
```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Настройка

Для production ориентир — `deploy/metrotherapy.env.example`. Реальные секреты хранить только на сервере, не в репозитории.

## Запуск
```bat
python main.py
```

## Smoke-check
```bat
python -m compileall .
python -c "from services.schema import init_db; init_db(); print('DB OK')"
```

## Продакшн-ядро (что уже закрыто)

1) ✅ БД и конкурентность
   - WAL + timeout + сериализация доступа в `services/db.py` для SQLite local/dev.
   - Postgres является обязательным production engine.

2) ✅ Единая проверка доступа/подписки
   - `services/access.py` — стабильный API для handlers.
   - `services/subscription.py` остаётся совместимым и не ломается.

3) ✅ Детерминированный scheduler (idempotent jobs)
   - `core/engine.py` атомарно забирает due-jobs через `services/jobs.claim_due_jobs()`.
   - при сетевых ошибках job возвращается в очередь с задержкой.
   - `services/scheduler.py` держит protected tick-contour: сбой одного owner-tick логируется и выводится в health/release-control, но не убивает весь background loop.

4) ✅ Воронка / аналитика / деньги
   - заготовки находятся в `services/funnel*.py`, `services/analytics.py`, `services/events.py`, `handlers/admin_*.py`.


## Импорты
- Внутри пакета `services/` используются только абсолютные импорты: `from services.xxx import ...`.
- Снаружи можно импортировать из единого API: `from services import db, init_db, store, has_access`.


## Проверка импортов
- Windows: `scripts\check_imports.bat`
- Linux/Mac: `bash scripts/check_imports.sh`

## Окружение и режимы валидации
Валидация контента/схемы запускается на старте приложения. Жёсткость управляется через env.

### Переменные окружения
```env
APP_ENV=prod|dev
VALIDATOR_STRICT=0|1
VALIDATOR_RELEASE_MODE=0|1
```

### Как это работает
- **APP_ENV**
  - `prod` → строгая валидация по умолчанию
  - `dev` → мягкая валидация по умолчанию
- **VALIDATOR_STRICT** (переопределяет APP_ENV)
  - `1` → строгий режим (гейтит запуск: при проблемах будет `ValidationError`)
  - `0` → soft-режим (пишет warning в логи, но не валит запуск)
- **VALIDATOR_RELEASE_MODE**
  - `1` → включает проверку «чистоты релиза» (нет `__pycache__/` и `*.pyc`)
  - `0` → проверка выключена

### Рекомендованные пресеты
**Прод:**
```env
APP_ENV=prod
TELEGRAM_TRANSPORT=polling
TELEGRAM_WEBHOOK_ENABLED=0
TELEGRAM_STARS_ENABLED=1
TELEGRAM_YOOKASSA_ENABLED=0
TELEGRAM_STARS_PRICING_MODE=explicit
METRO_DB_ENGINE=postgres
DATABASE_URL=postgresql://...
TOKEN_ENFORCEMENT_MODE=hard
VALIDATOR_RELEASE_MODE=1
VALIDATOR_GUARDRAILS_STRICT=1
```

**Dev:**
```env
APP_ENV=dev
VALIDATOR_STRICT=0
```

## CI-smoke и release gate
Минимальный smoke-набор для CI (быстро ловит поломки импортов/контрактов):

```bash
python -m compileall .
python scripts/validate_project.py
python -c "from services.validator import validate_all; validate_all(strict=False); print('OK')"
```

GitHub Actions дополнительно запускает полный regression gate:

```bash
python -m pytest -q -p no:cacheprovider
python scripts/check_ruff.py
python scripts/check_release_hygiene.py
```

## Production readiness stop-condition

Чтобы честно назвать deployment production-ready, должен проходить строгий gate без skip-флагов:

```bash
export METRO_RESTORE_DRILL_DATABASE_URL="postgresql://...safe_restore_target..."
python scripts/production_gate.py
```

Этот gate требует:

- full pytest;
- strict validator + smoke;
- storage/legacy SQLite audit;
- disaster recovery GREEN;
- Postgres restore drill на безопасной non-production БД;
- scheduler job probe;
- auto-audio dry-run probe;
- payment reconciliation/idempotency proof;
- synthetic user journey E2E proof;
- Telegram live smoke;
- `/health`/`/readyz` зелёные.

Админская поверхность: `/release` или `/release_gate` показывает storage, DR, обязательные proof-пробы, payment problems, stale auto-audio locks и scheduler watchdog fields.

# ClientPlatform production operations

Этот runbook относится к выделенному Docker Compose deployment в `deploy/clientplatform`.
Он не включает реальные секреты и не включает MAX, VK или рекламные расходы.

## Фактическая topology

- `postgres` — PostgreSQL 16;
- `app` — ClientPlatform runtime;
- `caddy` — публичный HTTPS reverse proxy;
- `backup` — профиль `operations` для зашифрованной резервной копии;
- `s3-replication` — отдельный профиль для off-site replication.

Внутри контейнерной сети приложение использует:

- `8181` — HTTP ingress для разрешённых webhook-native провайдеров, payment и OAuth;
- `8182` — внутренние `/healthz` и `/readyz`;
- `8191` — media gateway.

Публично Caddy открывает только 80/443. `/healthz` и `/readyz` снаружи должны возвращать 404. Telegram работает только через polling; его webhook-prefix снаружи также должен возвращать 404.

## 1. Обновление исходников

Команды выполняются в корне уже клонированного production-репозитория. Рекомендуемый путь ниже — `/opt/clientplatform`; при другом размещении замените только первую строку.

```bash
cd /opt/clientplatform
git fetch --prune origin
git checkout main
git pull --ff-only origin main
git status --short
git rev-parse HEAD
```

`git status --short` перед deploy должен быть пустым. Deploy выполняет текущий `HEAD`; отдельного аргумента `--ref` у скрипта нет.

## 2. Production environment

Основной файл:

```bash
deploy/clientplatform/clientplatform.env
```

Он должен быть обычным файлом, не symlink, с правами `0600`.

```bash
sudo test -f deploy/clientplatform/clientplatform.env
sudo test ! -L deploy/clientplatform/clientplatform.env
sudo chmod 600 deploy/clientplatform/clientplatform.env
sudo stat -c '%a %U:%G %n' deploy/clientplatform/clientplatform.env
```

Подготовка добавляет только отсутствующие безопасные defaults и никогда не заменяет существующие секреты:

```bash
sudo python3 scripts/clientplatform_prepare_production_env.py \
  deploy/clientplatform/clientplatform.env
```

Минимально должны быть заполнены реальные production-значения, включая dedicated domain, PostgreSQL, S3 credentials/bucket, Telegram bot identity, admin IDs и backup contract. `MAX_WEBHOOK_ENABLED=0`, `VK_WEBHOOK_ENABLED=0`, `CLIENTPLATFORM_AD_CONNECTIONS_ENABLED=0` и `CLIENTPLATFORM_AD_SPEND_MUTATIONS_ENABLED=0` являются безопасными defaults.

Файл `deploy/clientplatform/.env` необязателен. Compose подключает его только когда он уже существует; основной runtime env всегда `clientplatform.env`.

## 3. Fail-closed preflight

```bash
sudo python3 scripts/clientplatform_production_preflight.py \
  --env-file deploy/clientplatform/clientplatform.env
```

Успех заканчивается маркером:

```text
CLIENTPLATFORM_PRODUCTION_PREFLIGHT_OK
```

Ошибки необходимо исправить до deploy. Не отключайте polling contract, production isolation, encrypted backup или secret-reference checks ради прохождения preflight.

## 4. Production deploy

Предпочтительный режим требует `CLIENTPLATFORM_BACKUP_AGE_RECIPIENT` и создаёт зашифрованную pre-deploy backup:

```bash
sudo python3 scripts/clientplatform_production_deploy.py \
  --timeout-seconds 240
```

Скрипт последовательно:

1. блокирует параллельный deploy;
2. подготавливает и проверяет `clientplatform.env`;
3. проверяет текущую baseline-версию без требования нового route contract;
4. удаляет старые rollback/release Docker-теги и legacy `recovered-*`, сохраняя одно историческое поколение до создания нового rollback;
5. ограничивает весь неиспользуемый build-cache, сохраняя не более 2 GB для ускорения следующей сборки;
6. fail-closed проверяет диск: deploy запрещён при использовании от 75% или свободном месте менее 7 GiB; от 70% пишет предупреждение;
7. создаёт pre-deploy backup;
8. создаёт новый rollback, поэтому после успешного переключения доступны два последних rollback-поколения;
9. собирает `visual-gateway`, `app` и `backup`;
10. пересоздаёт `visual-gateway`, затем только `app` и `caddy`;
11. ждёт внутренние health/readiness/runtime markers;
12. проверяет точный публичный ответ `ClientPlatform`;
13. требует HTTP 404 на фактическом Telegram webhook-prefix;
14. пишет evidence в `/var/lib/clientplatform/deploy-evidence/latest.json`, включая disk/retention summary;
15. при неудаче автоматически восстанавливает предыдущий app image и повторно проверяет полный внешний контракт.

Успех:

```text
CLIENTPLATFORM_PRODUCTION_DEPLOY_OK:<evidence-path>
```

Аварийный plaintext backup допускается только осознанным explicit override, когда AGE recipient ещё не настроен:

```bash
sudo python3 scripts/clientplatform_production_deploy.py \
  --allow-local-backup \
  --timeout-seconds 240
```

Этот режим не является нормальной production-конфигурацией.

`--recover-unavailable-baseline` предназначен только для явно подтверждённого восстановления уже недоступной baseline-версии. В этом режиме безопасный автоматический rollback может быть невозможен:

```bash
sudo python3 scripts/clientplatform_production_deploy.py \
  --recover-unavailable-baseline \
  --timeout-seconds 240
```

## 5. Compose status и logs

```bash
cd /opt/clientplatform/deploy/clientplatform

COMPOSE=(sudo docker compose)
if [[ -f .env ]]; then
  COMPOSE+=(--env-file .env)
fi
COMPOSE+=(--env-file clientplatform.env -f compose.production.yml)

"${COMPOSE[@]}" ps
"${COMPOSE[@]}" logs --tail=200 app caddy postgres
```

Отдельного Compose-сервиса `bot_gateway` нет: managed bot gateway работает внутри `app`.

## 6. Внутренние health/readiness

```bash
sudo docker exec clientplatform-production-app-1 python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8182/healthz', timeout=5).read().decode())"

sudo docker exec clientplatform-production-app-1 python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8182/readyz', timeout=5).read().decode())"
```

Оба ответа должны быть JSON с `"ok": true`.

## 7. Внешний HTTPS contract

```bash
cd /opt/clientplatform/deploy/clientplatform
DOMAIN="$(sed -n 's/^CLIENTPLATFORM_DOMAIN=//p' clientplatform.env | tail -n 1)"

curl --proto '=https' --tlsv1.2 --fail --silent --show-error \
  --max-time 20 "https://${DOMAIN}/"
```

Ответ должен быть точно:

```text
ClientPlatform
```

Публичные health endpoints должны отсутствовать:

```bash
test "$(curl --proto '=https' --tlsv1.2 --silent --show-error \
  --output /dev/null --write-out '%{http_code}' --max-time 20 \
  "https://${DOMAIN}/healthz")" = "404"

test "$(curl --proto '=https' --tlsv1.2 --silent --show-error \
  --output /dev/null --write-out '%{http_code}' --max-time 20 \
  "https://${DOMAIN}/readyz")" = "404"
```

Telegram webhook-prefix берётся из env, а при пустом значении используется `/telegram-webhook`:

```bash
PREFIX="$(sed -n 's/^TELEGRAM_WEBHOOK_PREFIX=//p' clientplatform.env | tail -n 1)"
PREFIX="${PREFIX:-/telegram-webhook}"
STATUS="$(curl --proto '=https' --tlsv1.2 --silent --show-error \
  --output /dev/null --write-out '%{http_code}' --max-time 20 \
  --request POST \
  --header 'Content-Type: application/json' \
  --header 'X-Telegram-Bot-Api-Secret-Token: intentionally-invalid-operator-proof' \
  --data-binary '{}' \
  "https://${DOMAIN}${PREFIX}")"
test "${STATUS}" = "404"
```

Любой иной статус означает нарушение polling-only public contract.

## 8. Deploy evidence

```bash
sudo cat /var/lib/clientplatform/deploy-evidence/latest.json
```

Успешное evidence должно содержать текущий `target_sha`, `"ok": true`, `"telegram_transport": "polling"`, проверенный `telegram_webhook_prefix` и `"telegram_webhook_absent": true`.

## Запрещённые shortcuts

- не используйте `docker compose down` при обычном обновлении;
- не удаляйте volumes и PostgreSQL container вручную;
- не публикуйте наружу 8181, 8182 или 8191;
- не запускайте Telegram webhook параллельно с polling;
- не включайте MAX/VK или рекламные mutations без полного provider setup и отдельного owner consent;
- не передавайте deploy-скрипту несуществующие аргументы `--ref`, `--repository-root`, `--deploy-root`, `--env-file`, `--compose-file` или `--project-name`.

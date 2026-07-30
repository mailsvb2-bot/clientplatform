# ClientPlatform: зашифрованные PostgreSQL backup и offsite S3

## Цель

Контур создаёт PostgreSQL custom dump, проверяет SHA-256, шифрует его через age X25519, удаляет plaintext-комплект и при явном включении отправляет зашифрованный комплект в отдельный versioned backup bucket.

Порядок объектов в S3:

1. `*.dump.age` — ciphertext;
2. `*.dump.age.sha256` — контрольная сумма ciphertext;
3. `*.dump.age.json` — метаданные и commit-marker завершённого комплекта.

Metadata загружается последней. Комплект без metadata нельзя считать завершённым.

## Модель ключей

Backup worker получает только публичный age recipient вида `age1...`.

Приватная age identity:

- не хранится в `clientplatform.env`;
- не монтируется в постоянно работающий app-контейнер;
- не загружается в S3 рядом с backup;
- хранится отдельно с правами `0600`, желательно также в офлайн-копии.

Потеря private identity означает невозможность восстановления backup.

## Создание identity

В защищённой операторской среде:

```bash
umask 077
age-keygen -o /root/clientplatform-backup-age-identity.txt
chmod 600 /root/clientplatform-backup-age-identity.txt
age-keygen -y /root/clientplatform-backup-age-identity.txt
```

Последняя команда выводит публичный recipient. Только его нужно записать в production env:

```text
CLIENTPLATFORM_BACKUP_ENCRYPTION_REQUIRED=1
CLIENTPLATFORM_BACKUP_AGE_RECIPIENT=age1...
```

Private identity нужно скопировать во второе защищённое место до включения расписания.

## Локальное доказательство encrypted backup

Для Docker Compose deployment:

```bash
docker compose --env-file clientplatform.env -f compose.production.yml \
  --profile operations run --rm backup
```

Успех:

```text
CLIENTPLATFORM_ENCRYPTED_BACKUP_OK:/var/backups/clientplatform/postgres/clientplatform-...dump.age
```

В каталоге не должно остаться соответствующего `*.dump` plaintext-файла.

## Включение offsite S3

До proof держать:

```text
CLIENTPLATFORM_POSTGRES_BACKUP_S3_ENABLED=0
CLIENTPLATFORM_POSTGRES_BACKUP_FRESHNESS_REQUIRED=0
```

После проверки private versioned backup bucket установить только upload-флаг:

```text
CLIENTPLATFORM_POSTGRES_BACKUP_S3_ENABLED=1
CLIENTPLATFORM_POSTGRES_BACKUP_S3_PREFIX=postgres
CLIENTPLATFORM_POSTGRES_BACKUP_S3_EVIDENCE_DIR=/var/lib/clientplatform/postgres-backup-s3-evidence
CLIENTPLATFORM_POSTGRES_BACKUP_MAX_AGE_SECONDS=10800
CLIENTPLATFORM_POSTGRES_BACKUP_FRESHNESS_REQUIRED=0
```

Повторный ручной запуск backup pipeline должен дополнительно вывести:

```text
CLIENTPLATFORM_POSTGRES_BACKUP_S3_OK:/var/lib/clientplatform/postgres-backup-s3-evidence/latest.json
```

Uploader:

- не читает весь dump в память;
- использует фиксированные чанки и `Content-Length`;
- подписывает заранее вычисленный SHA-256 через SigV4;
- после PUT проверяет размер и SHA-256 через HEAD metadata;
- требует включённое versioning у backup bucket;
- не принимает plaintext `*.dump`.

После успешного upload-proof включить freshness gate:

```text
CLIENTPLATFORM_POSTGRES_BACKUP_FRESHNESS_REQUIRED=1
```

Ручная проверка:

```bash
bash deploy/clientplatform/run-postgres-backup-operation.sh freshness
```

Успех:

```text
CLIENTPLATFORM_POSTGRES_BACKUP_FRESHNESS_OK
```

Freshness-check проверяет owner-only `latest.json`, age-шифрование, три объекта комплекта, timestamp и максимальный возраст. Сбой freshness не перезапускает приложение и не останавливает Telegram-бот; красным становится отдельный operations service.

## Установка systemd timers для Docker deployment

Unit-файлы рассчитаны на production Compose в `/opt/clientplatform/deploy/clientplatform`. Они запускаются от root, потому что доступ к Docker socket эквивалентен root-доступу. App-контейнер и его runtime по-прежнему работают непривилегированно.

```bash
install -m 0644 deploy/clientplatform/clientplatform-postgres-backup.service \
  /etc/systemd/system/clientplatform-postgres-backup.service
install -m 0644 deploy/clientplatform/clientplatform-postgres-backup.timer \
  /etc/systemd/system/clientplatform-postgres-backup.timer
install -m 0644 deploy/clientplatform/clientplatform-postgres-backup-freshness.service \
  /etc/systemd/system/clientplatform-postgres-backup-freshness.service
install -m 0644 deploy/clientplatform/clientplatform-postgres-backup-freshness.timer \
  /etc/systemd/system/clientplatform-postgres-backup-freshness.timer
systemctl daemon-reload
```

До включения timers выполнить оба oneshot вручную:

```bash
systemctl start clientplatform-postgres-backup.service
systemctl status --no-pager clientplatform-postgres-backup.service
systemctl start clientplatform-postgres-backup-freshness.service
systemctl status --no-pager clientplatform-postgres-backup-freshness.service
```

После двух успешных запусков:

```bash
systemctl enable --now clientplatform-postgres-backup.timer
systemctl enable --now clientplatform-postgres-backup-freshness.timer
systemctl list-timers --all 'clientplatform-postgres-backup*'
```

Расписание:

- encrypted backup — каждый час с jitter до пяти минут;
- freshness-check — каждые 15 минут с jitter до минуты;
- `Persistent=true` восполняет пропущенный запуск после выключения сервера;
- обе службы используют один `flock`, поэтому freshness не читает evidence во время backup/upload;
- backup ограничен 45 минутами, freshness — 15 минутами.

## Restore drill

Для локального ciphertext:

```bash
python -m scripts.clientplatform_postgres_backup_crypto decrypt \
  /var/backups/clientplatform/postgres/clientplatform-...dump.age \
  --identity /root/clientplatform-backup-age-identity.txt \
  --output /tmp/clientplatform-restore.dump
```

Команда проверяет plaintext SHA-256 из зашифрованных метаданных и автоматически создаёт рядом owner-only манифест:

```text
/tmp/clientplatform-restore.dump.sha256
```

Затем административный DSN передаётся только на время drill:

```bash
CLIENTPLATFORM_RESTORE_ADMIN_DATABASE_URL='postgresql://...' \
python -m scripts.clientplatform_postgres_backup restore-drill \
  /tmp/clientplatform-restore.dump
rm -f \
  /tmp/clientplatform-restore.dump \
  /tmp/clientplatform-restore.dump.sha256
```

Успех подтверждается маркером:

```text
CLIENTPLATFORM_RESTORE_DRILL_OK:/var/lib/clientplatform/restore-evidence/restore-....json
```

Restore drill создаёт одноразовую базу, проверяет обязательные таблицы и всегда удаляет временную базу. Временный расшифрованный dump и его checksum после проверки также удаляются оператором.

## Fail-closed свойства

Pipeline завершится ошибкой и не объявит backup успешным, если:

- encryption flag выключен;
- recipient отсутствует или имеет неподдерживаемый формат;
- `pg_dump` завершился ошибкой;
- checksum plaintext не совпал;
- age не создал ciphertext;
- versioning backup bucket выключен;
- S3 PUT или последующая HEAD-проверка не прошли;
- metadata комплекта не соответствует ciphertext.

Freshness-check завершится ошибкой, если:

- freshness обязателен, но offsite upload выключен;
- `latest.json` отсутствует, является symlink или доступен группе/прочим пользователям;
- evidence не относится к `postgres_backup_s3_upload`;
- комплект не age-зашифрован или содержит не три объекта;
- timestamp находится слишком далеко в будущем;
- возраст последней внешней копии превышает лимит.

При ошибке шифрования plaintext-комплект удаляется. При ошибке offsite upload локальный зашифрованный комплект сохраняется для повторной отправки.

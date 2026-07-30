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

## Локальное доказательство encrypted backup

Для Docker Compose deployment:

```bash
docker compose --env-file clientplatform.env -f compose.production.yml \
  --profile operations run --rm --no-deps backup
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
```

После проверки private versioned backup bucket и успешной ручной загрузки установить:

```text
CLIENTPLATFORM_POSTGRES_BACKUP_S3_ENABLED=1
CLIENTPLATFORM_POSTGRES_BACKUP_S3_PREFIX=postgres
CLIENTPLATFORM_POSTGRES_BACKUP_S3_EVIDENCE_DIR=/var/lib/clientplatform/postgres-backup-s3-evidence
```

Повторный запуск backup pipeline должен дополнительно вывести:

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

## Restore drill

Для локального ciphertext:

```bash
python scripts/clientplatform_postgres_backup_crypto.py decrypt \
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
python scripts/clientplatform_postgres_backup.py restore-drill \
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

При ошибке шифрования plaintext-комплект удаляется. При ошибке offsite upload локальный зашифрованный комплект сохраняется для повторной отправки.

## Что этот контур пока не делает

Этот этап создаёт безопасный encrypted pipeline и optional offsite upload. Периодический systemd timer и автоматическая проверка возраста последнего offsite backup вводятся отдельным изменением после live proof, чтобы не включать расписание до подтверждения ключей и bucket policy.

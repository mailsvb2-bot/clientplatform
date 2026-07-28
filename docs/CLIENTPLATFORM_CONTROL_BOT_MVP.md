# ClientPlatform Telegram Control Bot MVP

## Назначение

Этот режим превращает существующего Telegram-бота в центральную админку специалиста. Он не активируется автоматически и не меняет унаследованный интерфейс, пока оператор явно не включит флаг.

## Включение

```env
CLIENTPLATFORM_CONTROL_BOT_ENABLED=1
```

Используется обычный `BOT_TOKEN`, уже необходимый приложению. При включении ClientPlatform безопасно предоставляет его dispatch runtime через внутреннюю ссылку:

```text
secret://env/CLIENTPLATFORM_SECRET_CONTROL_TELEGRAM_BOT_TOKEN
```

Отдельно копировать токен в БД или передавать его пользователям не требуется.

Если не задано иное, control mode включает ClientPlatform dispatch worker. Явное значение имеет приоритет:

```env
CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED=0
```

## Пользовательский маршрут

1. Владелец отправляет `/start`.
2. Пишет название бизнеса.
3. Своими словами описывает деятельность.
4. Подключает один или несколько модулей:
   - программы и материалы;
   - консультации;
   - услуги;
   - собственный формат.
5. Для консультаций, услуг и собственного формата добавляет предложения.
6. Для программы отправляет название, название первого урока и Telegram-аудио, видео, документ, изображение или текст.
7. Создаёт одноразовую ссылку клиента.
8. Клиент открывает ссылку и становится tenant-scoped Customer с Telegram identity.
9. Владелец выбирает программу и клиента.
10. ClientPlatform создаёт Enrollment, logical Delivery и dispatch outbox; worker отправляет материал и сохраняет результат.

## Граница MVP

Первая версия намеренно использует общий Telegram-бот с deep links. Managed Client Bots, расписание консультаций, платежи, Mini App и дополнительные каналы подключаются следующими connector-модулями, не меняя основную модель бизнеса.

Production deployment этим документом не разрешается и должен выполняться отдельным решением владельца.


## Canonical rollout

ClientPlatform control bot and its dispatch runtime are enabled by default.
The only supported emergency rollback is explicit:

```text
CLIENTPLATFORM_CONTROL_BOT_ENABLED=0
CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED=0
```

An absent flag never silently returns users to the imported legacy interface.
Consultations, services and custom offerings can publish tenant-safe booking
slots; connected Telegram customers can open their client portal and reserve an
available slot atomically.

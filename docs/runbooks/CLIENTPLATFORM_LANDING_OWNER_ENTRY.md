# ClientPlatform landing owner entry

Публичный лендинг владельца должен использовать только стабильные URL ClientPlatform:

- Telegram: `https://app.clientplatform.ru/clientplatform/open/telegram`
- ВКонтакте: `https://app.clientplatform.ru/clientplatform/open/vk`
- MAX: `https://app.clientplatform.ru/clientplatform/open/max`

Не вставляйте в Tilda прямые `t.me`, `vk.com` или `max.ru` deep link. Runtime сам строит provider URL с owner payload `cpo_landing` (Telegram через `start`, VK через `ref`, MAX через query-параметр `payload`). Это отделяет вход владельца от customer acquisition (`cpa_*`) и customer invite (`cpj_*`).

Если официальный provider ещё не настроен или его webhook-runtime выключен, стабильный URL отвечает HTTP 503 и не отправляет пользователя в неработающий бот. После добавления production credentials и включения provider тот же URL автоматически начинает отдавать HTTP 302 в официальный бот/сообщество; повторно менять Tilda не требуется.

## Tilda

В обоих блоках с кнопками мессенджеров замените текущие значения:

- Telegram `https://t.me/clientplatform_bot?start=cpa_...` -> стабильный Telegram URL выше;
- `ССЫЛКА_CLIENTPLATFORM_VK` -> стабильный VK URL выше;
- `ССЫЛКА_CLIENTPLATFORM_MAX` -> стабильный MAX URL выше.

После публикации проверьте HTML `https://clientplatform.ru/`: в `href` не должно оставаться `ССЫЛКА_CLIENTPLATFORM_`, `cpa_` у owner-кнопок или прямых provider URL.

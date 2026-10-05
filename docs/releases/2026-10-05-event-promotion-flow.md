# Единый маршрут продвижения вебинара

Дата: 2026-10-05  
PR: #576

## Проблема

До этого event funnel и promotion funnel существовали рядом, но были связаны неверно.
Telegram-кнопка «Запустить рекламу» после анонса вебинара открывала старый
`cpj:promote`, который рекламирует `booking_slot_id` услуги. Контекст
`event_id` терялся. В VK/MAX при этом не было сопоставимого пути от анонса к
рекламе и визуалам.

Подменять вебинар услугой запрещено архитектурой: `promotion_campaigns` требует
`offering_id + booking_slot_id`, а event registrations имеют собственный
канонический CRM/notification/attendance lifecycle.

## Решение

Добавлен event-scoped promotion orchestration. Каноническая цепочка:

`креатив / объявление -> /e/<slug> -> registration -> reminders -> video service -> follow-up`.

Рекламная ссылка события строится как существующий public event endpoint с:

- `source=ads`;
- `campaign_ref=event:<event_id>`.

Новая ссылка не использует `/clientplatform/acquire` и не создаёт фиктивный
booking slot.

## UI

В Telegram, VK и MAX контент-план конкретного вебинара теперь содержит
«📢 Продвижение вебинара».

Экран продвижения показывает:

- состояние продающего лендинга;
- состояние площадки эфира;
- все активные регистрации;
- регистрации с `source=ads`;
- точную рекламную event-ссылку;
- переходы к лендингу, анонсу/креативу, Яндекс Директу, площадке эфира и
  контент-плану.

После создания вебинара этот путь доступен сразу.

## Креативы

Анонс остаётся event-scoped. Если для event-day выбран режим картинки или видео,
подготовка визуала создаёт существующий durable creative receipt с
`binding.type=event_content`. Недоступность визуального провайдера не скрывает
сам текст анонса.

VK/MAX используют тот же native event contract. Если конкретное создание медиа
требует Telegram presentation surface, UI переключает только presentation
surface; event binding уже подготовлен и не теряется.

## Яндекс Директ

Экран «Яндекс Директ · вебинар» всегда показывает точную landing URL события и
ведёт к существующим защищённым контурам:

- рекламные кабинеты / OAuth;
- бюджет и безопасный запуск.

Старый `PromotionCampaign` не расширяется и не мигрируется в этом изменении:
он остаётся контрактом рекламы booking slots. Это предотвращает смешение
event registrations с service bookings.

## Атрибуция

Event registrations уже сохраняют `source` и `campaign_ref`. Новый dashboard
считает регистрации непосредственно из `clientplatform_event_registrations`
для текущего tenant + event.

Это означает, что метрика «Из рекламы» относится к вебинару, а не к соседней
услуге или свободному времени.

## Безопасность и совместимость

- нет destructive schema migration;
- нет второго event runtime;
- нет второй registration form;
- нет фиктивных offering/booking records;
- существующие booking-slot promotion campaigns работают без изменений;
- navigation callbacks `cpev:promote` и `cpev:py` repeatable и не считаются
  one-shot mutation;
- бюджет по-прежнему запускается только через существующее отдельное owner
  consent + launch действие;
- публичная event registration и event landing остаются единой точкой входа.

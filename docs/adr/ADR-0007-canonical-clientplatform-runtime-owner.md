# ADR-0007: Канонический владелец clientplatform runtime в процессе приложения

## Статус

Принято.

## Контекст

После ADR-0006 clientplatform получил безопасный Telegram HTTP transport, signed-media resolver, bounded dispatch scheduler и явные функции `start_clientplatform_runtime` / `stop_clientplatform_runtime`. Однако runtime оставался только отдельно вызываемым контуром: production application lifecycle его не создавал. Даже при `CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED=1` записи outbox не обрабатывались автоматически.

Прямой запуск при импорте запрещён: к этому моменту может отсутствовать event loop, канонический `TaskManager` или полная clientplatform-схема. Отдельный scheduler вне `TaskManager` также нарушил бы single-owner shutdown contract импортированного baseline.

## Решение

`services.bg.bind_task_manager` подключает один `clientplatform-runtime-owner` к тому же `TaskManager`, которым владеют остальные фоновые задачи процесса.

Owner:

1. полностью бездействует, пока `CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED` выключен;
2. ожидает наличие полного набора additive clientplatform tables;
3. fail-closed завершает задачу с диагностируемой ошибкой, если схема не готова за ограниченное время;
4. создаёт ровно один `ClientPlatformDispatchScheduler`;
5. удерживает lifecycle ownership до shutdown;
6. в `finally` вызывает `stop_clientplatform_runtime`;
7. автоматически останавливается при graceful shutdown и self-heal restart через канонический `TaskManager`.

Отключённый scheduler больше не отмечается как `clientplatform_runtime_composed`.

## Почему не изменён production startup напрямую

Точка `bind_task_manager` уже является канонической границей владения фоновыми задачами и вызывается внутри активного event loop до запуска runtime-компонентов. Это позволяет сделать additive wiring без переноса clientplatform в legacy handlers, без отдельного процесса и без использования инфраструктуры Метротерапии.

## Риски и ограничения

- clientplatform dispatch по-прежнему выключен по умолчанию.
- Включение требует отдельной clientplatform-конфигурации, секретов и базы; production-конфигурация Метротерапии запрещена.
- Media gateway и object-storage ingress остаются отдельной инфраструктурной границей.
- Это подключает готовый dispatch contour, но не объявляет весь первый вертикальный сценарий или MVP завершёнными.

## Проверки

- disabled owner не обращается к БД и не занимает lifecycle;
- проверяется полный набор clientplatform tables;
- owner стартует только после schema readiness;
- cancellation гарантированно вызывает stop;
- один `TaskManager` владеет owner-задачей;
- schema timeout fail-closed;
- clientplatform Boundary Diagnostics запускает новый regression wall.

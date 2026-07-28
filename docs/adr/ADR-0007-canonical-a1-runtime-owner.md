# ADR-0007: Канонический владелец A1 runtime в процессе приложения

## Статус

Принято.

## Контекст

После ADR-0006 A1 получил безопасный Telegram HTTP transport, signed-media resolver, bounded dispatch scheduler и явные функции `start_a1_runtime` / `stop_a1_runtime`. Однако runtime оставался только отдельно вызываемым контуром: production application lifecycle его не создавал. Даже при `A1_DISPATCH_RUNTIME_ENABLED=1` записи outbox не обрабатывались автоматически.

Прямой запуск при импорте запрещён: к этому моменту может отсутствовать event loop, канонический `TaskManager` или полная A1-схема. Отдельный scheduler вне `TaskManager` также нарушил бы single-owner shutdown contract импортированного baseline.

## Решение

`services.bg.bind_task_manager` подключает один `a1-runtime-owner` к тому же `TaskManager`, которым владеют остальные фоновые задачи процесса.

Owner:

1. полностью бездействует, пока `A1_DISPATCH_RUNTIME_ENABLED` выключен;
2. ожидает наличие полного набора additive A1 tables;
3. fail-closed завершает задачу с диагностируемой ошибкой, если схема не готова за ограниченное время;
4. создаёт ровно один `A1DispatchScheduler`;
5. удерживает lifecycle ownership до shutdown;
6. в `finally` вызывает `stop_a1_runtime`;
7. автоматически останавливается при graceful shutdown и self-heal restart через канонический `TaskManager`.

Отключённый scheduler больше не отмечается как `a1_runtime_composed`.

## Почему не изменён production startup напрямую

Точка `bind_task_manager` уже является канонической границей владения фоновыми задачами и вызывается внутри активного event loop до запуска runtime-компонентов. Это позволяет сделать additive wiring без переноса A1 в legacy handlers, без отдельного процесса и без использования инфраструктуры Метротерапии.

## Риски и ограничения

- A1 dispatch по-прежнему выключен по умолчанию.
- Включение требует отдельной A1-конфигурации, секретов и базы; production-конфигурация Метротерапии запрещена.
- Media gateway и object-storage ingress остаются отдельной инфраструктурной границей.
- Это подключает готовый dispatch contour, но не объявляет весь первый вертикальный сценарий или MVP завершёнными.

## Проверки

- disabled owner не обращается к БД и не занимает lifecycle;
- проверяется полный набор A1 tables;
- owner стартует только после schema readiness;
- cancellation гарантированно вызывает stop;
- один `TaskManager` владеет owner-задачей;
- schema timeout fail-closed;
- A1 Boundary Diagnostics запускает новый regression wall.

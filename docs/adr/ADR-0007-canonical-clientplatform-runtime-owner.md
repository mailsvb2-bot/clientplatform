# ADR-0007: Канонический владелец clientplatform runtime в процессе приложения

## Статус

Принято.

## Контекст

После ADR-0006 clientplatform получил безопасный Telegram HTTP transport, signed-media resolver, bounded dispatch scheduler и явные функции `start_clientplatform_runtime` / `stop_clientplatform_runtime`. Однако runtime оставался только отдельно вызываемым контуром: production application lifecycle его не создавал. Даже при `CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED=1` записи outbox не обрабатывались автоматически.

Прямой запуск при импорте запрещён: к этому моменту может отсутствовать event loop, канонический `TaskManager` или полная clientplatform-схема. Отдельный scheduler вне `TaskManager` также нарушил бы single-owner shutdown contract импортированного baseline.

Проверенный запуск новой production-базы на Timeweb выявил дополнительную гонку жизненного цикла: owner создаётся до startup-hook, а `init_db()` выполняется внутри startup-hook после сетевой подготовки Telegram. Если подготовка занимает дольше окна ожидания схемы, owner завершался с `clientplatform_runtime_schema_timeout`; приложение продолжало работать, но dispatch-runtime требовал ручного второго рестарта. Такое поведение не было фактически fail-closed и оставляло production в частично запущенном состоянии.

## Решение

`services.bg.bind_task_manager` подключает один `clientplatform-runtime-owner` к тому же `TaskManager`, которым владеют остальные фоновые задачи процесса.

Owner:

1. полностью бездействует, пока `CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED` выключен;
2. ожидает наличие полного набора additive clientplatform tables;
3. после ограниченного интервала пишет диагностическое предупреждение, но не теряет lifecycle ownership и продолжает ожидание схемы;
4. автоматически запускается, как только схема становится готова, без перезапуска процесса;
5. создаёт ровно один `ClientPlatformDispatchScheduler`;
6. удерживает lifecycle ownership до shutdown;
7. в `finally` вызывает `stop_clientplatform_runtime`;
8. автоматически останавливается при graceful shutdown и self-heal restart через канонический `TaskManager`.

Отключённый scheduler больше не отмечается как `clientplatform_runtime_composed`.

## Почему не изменён production startup напрямую

Точка `bind_task_manager` уже является канонической границей владения фоновыми задачами и вызывается внутри активного event loop до запуска runtime-компонентов. Сохранение owner-задачи во время инициализации схемы устраняет гонку для Docker, systemd и прямого запуска приложения без дублирования schema bootstrap в нескольких deployment entrypoint.

## Риски и ограничения

- clientplatform dispatch по-прежнему выключен по умолчанию.
- Включение требует отдельной clientplatform-конфигурации, секретов и базы; production-конфигурация других продуктов запрещена.
- Если схема действительно никогда не станет готова, owner останется бездействующим и будет выдавать ограниченные по частоте предупреждения вместо бесконечного restart-loop.
- Media gateway и object-storage ingress остаются отдельной инфраструктурной границей.
- Это подключает готовый dispatch contour, но не объявляет весь первый вертикальный сценарий или MVP завершёнными.

## Проверки

- disabled owner не обращается к БД и не занимает lifecycle;
- проверяется полный набор clientplatform tables;
- owner стартует только после schema readiness;
- owner переживает истечение диагностического окна и стартует при последующей готовности схемы;
- cancellation гарантированно вызывает stop;
- один `TaskManager` владеет owner-задачей;
- clientplatform Boundary Diagnostics запускает regression wall.

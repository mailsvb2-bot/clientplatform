# ADR-0124: canonical tenant-scoped AutomationPolicy foundation

**Статус:** предложено патчем

## Контекст

ClientPlatform уже имеет специализированные safety barriers для ad spend, Sales AI, consent-bound messaging и approval-only Growth Apply. Safe Autopilot требует единого верхнеуровневого policy contract, но не второго automation engine, scheduler или источника бизнес-истины.

## Решение

1. `AutomationPolicy` становится versioned tenant-scoped policy ledger с immutable canonical JSON hash и явным lifecycle `draft -> approved -> superseded/revoked`.
2. Draft может создавать только роль, способная управлять бизнесом; утверждение exact policy hash разрешено только текущему OWNER после повторного server-side tenant/RBAC resolve.
3. Policy описывает explicit allow/deny actions, channels, audiences, schedule/quiet hours, money/AI limits, approval thresholds, content restrictions, stop conditions и expiry.
4. `PolicyCheck` является чистой детерминированной проверкой с результатом `allow`, `approval_required` или `deny`. Отсутствующий, просроченный, cross-tenant или недостаточно доказанный policy state закрывается fail-closed.
5. Общая policy не отменяет специализированные guards. Ad-spend consent/caps, Sales AI consent и provider-specific safety остаются обязательными дополнительными барьерами.
6. M5-001 не вводит external execution. Текущая owner-кнопка Autopilot создаёт policy, разрешающий только `growth.read_only_analysis`; money/provider writes этим переключателем не разрешаются.
7. Версии сериализуются transaction-scoped lock на canonical `businesses` row. PostgreSQL CI доказывает, что конкурентный draft с одним expected version и конкурентный owner approval дают ровно одного победителя и один audit event.
8. Legacy `business_admin_settings.autopilot_enabled` перестаёт быть источником истины; UI читает effective `AutomationPolicy`.

## Следствия

Следующие Safe Autopilot slices получают один policy boundary для CandidateAction, но ни один будущий executor не может трактовать режим Autopilot как универсальное разрешение на внешнюю запись или деньги. Любая новая чувствительная capability требует явного расширения policy и нового owner approval.

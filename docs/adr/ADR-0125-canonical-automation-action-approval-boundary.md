# ADR-0125: canonical automation action approval boundary

**Статус:** предложено патчем

## Контекст

M5-001 ввёл единый tenant-scoped `AutomationPolicy` и детерминированный `PolicyCheck`, но намеренно остановился до owner approval конкретного автоматического действия. Следующий шаг Safe Autopilot должен сохранять точный `CandidateAction`, позволять владельцу явно принять или отклонить решение и выдавать проверяемое разрешение для будущего execution slice. При этом нельзя создавать второй approval engine, provider-specific workflow, scheduler или новый источник бизнес-истины.

## Решение

1. Action approval хранится в том же canonical automation contour рядом с `clientplatform_automation_policies` как `clientplatform_automation_action_approvals`; отдельный automation brain/store не вводится.
2. Approval intent содержит exact canonical `CandidateAction` JSON + SHA-256 fingerprint, business-scoped idempotency key, exact `AutomationPolicy` id/version/hash, причины `approval_required`, requester evidence и expiry.
3. Intent создаётся только если повторный server-side `PolicyCheck` текущей effective policy возвращает именно `approval_required`. `allow` нельзя искусственно превратить в approval, `deny` нельзя обойти через owner click.
4. Exact replay одного business-scoped idempotency key возвращает одну durable запись. Тот же key с другим candidate/policy/reasons закрывается fail-closed.
5. Читать текущие approval intents могут только разрешённые automation roles текущего бизнеса. `approve`, `reject` и `revoke` выполняет только текущий OWNER после повторного server-side tenant/RBAC resolve.
6. Owner approve перед mutation заново проверяет expiry, current effective policy id/version/hash и повторный `PolicyCheck`. Изменившаяся/revoked/expired policy или изменившаяся action semantics делают старый approval неавторитетным.
7. Approved intent может породить только детерминированный internal `AutomationActionAuthorization`, привязанный к exact approval/candidate/policy/owner evidence. Это не bearer-token и не provider credential; он действителен только после повторной server-side проверки текущей policy.
8. Reject и revoke являются durable owner decisions и журналируются в существующем `clientplatform_admin_audit_events`. Повтор того же решения идемпотентен; conflicting concurrent decision даёт ровно одного победителя.
9. M5-002 не выполняет provider calls, autonomous scheduling, message send, ad mutation, refund или любое money movement. Execution/verification/outcome остаются отдельным последующим slice.
10. Для money-bearing approval используется существующий canonical ISO-4217 settlement validator; неизвестная или несчётная валюта не доходит до owner approval UI.

## Следствия

Будущий executor не сможет использовать факт «Autopilot включён» или старый owner click как универсальное разрешение. Перед любым внешним действием он должен получить точный candidate, актуальный `PolicyCheck` и, где требуется, действующий authorization artifact, после чего всё равно пройти специализированные provider/consent/idempotency guards. Изменение policy автоматически обесценивает старое разрешение без скрытого переноса authority на новую версию.

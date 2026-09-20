# ADR-0130 — UCR attendance as the first real UIII observation source

**Статус:** принято реализацией vertical slice  
**Дата:** 2026-09-20

## Контекст

Первые три UIII consumer slices в ClientPlatform добавили provenance-bearing external
observations, exact revision/retraction/freshness semantics и owner feedback/value proof.
Следующий шаг по UIII Canon должен использовать не искусственный пример, а реальный
дополнительный источник, который даёт ClientPlatform новый проверяемый факт.

Universal Communication Runtime (UCR) теперь публикует business-neutral
`ucr.v1.UniversalConferenceService`. В exact revision
`22d598e057769d59fe0aa47e169cf2eb904abb18` публичный контракт содержит
`GetParticipantAttendance`, который возвращает read-only attendance projection по
каноническому Event journal UCR. Этот UCR SHA прошёл upstream CI, Conformance,
Phase 42, Phase 43, Phase 44 Supply Chain и Phase 45 Production Hardening.

Предыдущий ClientPlatform UCR boundary был намеренно закреплён на более старом SHA
`8097b41e69634c944c225f7071e80b991d4ddc02`, поэтому UniversalConferenceService
через gateway был недоступен.

## Решение

1. ClientPlatform обновляет exact UCR pin до
   `22d598e057769d59fe0aa47e169cf2eb904abb18`.
2. Sidecar остаётся transport-only adapter и по UniversalConferenceService открывает
   только два reviewed read RPC:
   - `GetParticipantAttendance`;
   - `GetCapabilities`.
3. Все Universal Conference mutations, включая создание конференции, изменение
   участников, prepare runtime и join-grant mutation, остаются закрыты в этом slice.
4. Application adapter читает exact canonical UCR request и строго нормализует
   attendance response. Он не достраивает UCR-owned conference/participant state.
5. Подтверждённое участие может быть преобразовано в
   `ExternalProductObservation(kind="ucr.conference_attendance")` с
   `SOURCE_VERIFIED` quality.
6. Перед созданием `SOURCE_VERIFIED` observation adapter сравнивает echoed
   `attendance.externalUserId` с participant bytes из exact canonical request.
   Mismatch fail-closed, поэтому attendance другого участника нельзя приписать Customer.
7. Observation provenance содержит только SHA-256 fingerprint canonical request и
   нормализованных attendance facts. Raw `externalUserId` не переносится в label,
   provenance или observation metadata.
8. Source timestamp берётся только из UCR attendance projection
   (`firstJoinAtUnixMs`, `firstMediaReadyAtUnixMs`, `lastLeaveAtUnixMs`).
   Adapter не подставляет локальный `now` как якобы source-observed time.
9. Нулевое attendance остаётся отдельным подтверждённым нулём в read model, но не
   превращается автоматически в observation «не пришёл». Это не позволяет смешать
   «0», «не наблюдалось» и «нет данных».
10. Live attendance помечается limitation: итоговая длительность может измениться.
   Отсутствие media-ready evidence также показывается как limitation, а не
   интерпретируется как проблема участника.
11. UCR unavailable/rejected/malformed response fail-closed и не создаёт фиктивный
    observation.
12. Этот slice не пишет attendance автоматически в Customer/CRM, не меняет
    CustomerIdentity, Sales stage, Next Best Action, AI prompt, follow-up, consent или
    AutomationPolicy.
13. Каждая новая attendance snapshot для уже существующего observation key получает
    exact next revision и supersedes текущий external event. Caller обязан передать
    canonical current head; устаревший head затем дополнительно fail-closed проверяется
    существующим ExternalProductRepository при persistence.
14. Existing Telegram/VK/MAX/email/SMS/web-chat остаются независимыми; UCR gateway
    по-прежнему disabled by default.

## Почему это UIII, а не второй communication brain

UCR остаётся владельцем conference/realtime/attendance semantics и Event journal.
ClientPlatform не копирует UCR attendance database и не вычисляет собственную
«истину посещения». UIII consumer adapter лишь переводит публичный проверяемый факт
в общий observation contract ClientPlatform с provenance и limitations.

Business action остаётся у ClientPlatform. Сам факт «участвовал 42 минуты» не является
разрешением отправить сообщение, изменить этап продажи или автоматически решить, что
клиент заинтересован.

## Измеримая ценность

Этот источник подходит для первого настоящего UIII value proof, потому что добавляет
факт, которого обычная регистрация на webinar не содержит: реальное подтверждённое
подключение и длительность присутствия.

После реального rollout можно измерять owner feedback по уже существующему #388
контру: useful / incorrect / wrong_customer, freshness и retraction. До production
источника никакой uplift не объявляется.

## Deployment boundary

Изменение готовит contract и adapter, но не включает UCR в production:

- sidecar не добавляется автоматически в production compose;
- `CLIENTPLATFORM_UCR_GATEWAY_ENABLED` не включается;
- production UCR listener/credentials/grants не создаются;
- никакие attendance pollers/jobs не запускаются.

Production activation остаётся отдельным reviewed rollout после доказательства
UCR endpoint, Service Principal permissions, TLS/network placement и observability.

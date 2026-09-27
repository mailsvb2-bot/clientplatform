# ADR-0132 — Provider-neutral conference gateway and reviewed UCR mutations

**Статус:** accepted by explicit owner task and implementation review  
**Дата:** 2026-09-27

## Контекст

Владелец передал проверенный набор патчей conference provider gateway и прямо поручил
оценить его практическую ценность и, если решение полезно, умно внедрить в ClientPlatform.
Это является явным owner decision на данный архитектурный slice.

ClientPlatform уже имеет канонический Event/EventSession domain, registration, CRM,
consent, notifications и provider-neutral shared HTTPS room links. UCR уже подключён
через exact revision `22d598e057769d59fe0aa47e169cf2eb904abb18`, но ADR-0129/0130
намеренно оставляли UniversalConferenceService mutations закрытыми до отдельного
reviewed решения.

В закреплённом UCR protobuf фактически опубликованы versioned RPC для conference
creation/resolution, lifecycle, entry gate, participants, runtime preparation,
single-use join grant, attendance и capabilities. Поэтому ClientPlatform может
получить managed conference provider без копирования UCR domain state.

## Решение

1. ClientPlatform вводит provider-neutral `ConferenceProvider` boundary. Он не владеет
   Event/EventSession/registration/CRM/consent/business state.
2. UCR является одним managed provider за этим boundary и остаётся владельцем
   conference/runtime/device semantics.
3. Для exact pinned revision дополнительно разрешается reviewed subset:
   - `CreateConference`, `ResolveConference`, `GetConference`;
   - `TransitionConference`, `SetEntryOpen`;
   - `EnsureParticipant`, `EnsureParticipantDevice`;
   - `UpdateParticipant`, `RemoveParticipant`, `ListParticipants`;
   - `PrepareConferenceRuntime`;
   - `IssueJoinGrant`, `RevokeJoinGrant`;
   - ранее разрешённые `GetParticipantAttendance`, `GetCapabilities`.
4. Все mutating RPC проходят существующий gateway idempotency contract и exact
   service/method/revision response binding.
5. Неизвестный service/method по-прежнему блокируется до gRPC I/O.
6. UCR join выдаётся только если capabilities одновременно подтверждают browser
   realtime gateway, production WebRTC и TURN. Иначе fail-closed.
7. UCR attendance обязан echo-ить того же `externalUserId`, который был запрошен.
   Mismatch или malformed protobuf-JSON bytes fail-closed до построения
   ClientPlatform attendance result.
8. Link-only HTTPS provider разрешён для уже созданной внешней комнаты, но не
   имитирует lifecycle/participants/runtime/attendance. Неподдерживаемые операции
   fail-closed.
9. Provider references нельзя использовать между разными providers.
10. Этот ADR не заменяет Event/EventSession и не создаёт второй webinar engine.
11. Shared `EventSession.join_url` не используется для хранения UCR single-use
    participant grants. Будущий user-facing UCR join должен выпускать grant на
    authenticated registration redirect boundary.
12. Gateway остаётся disabled by default. Этот ADR не включает production deploy и
    не утверждает readiness платных UCR-hosted вебинаров.

## Почему это не второй brain

ClientPlatform определяет бизнес-смысл события, tenant/RBAC, регистрацию, согласия,
уведомления и дальнейшие действия. UCR выполняет только realtime/conference provider
семантику через публичный versioned API. Provider gateway переводит один контракт в
другой и не хранит параллельную истину о мероприятии.

## Безопасность и failure semantics

- exact UCR SHA pin остаётся неизменным;
- HTTPS/secret-reference/bounded I/O guards сохраняются;
- mutation без idempotency key блокируется;
- read с mutation idempotency header блокируется;
- cross-provider reference блокируется до I/O;
- join TTL ограничен;
- production realtime readiness проверяется до join grant;
- attendance participant identity mismatch блокируется;
- link-only unsupported management fail-closed;
- никаких production credentials или токенов в репозитории.

## Последствия

ClientPlatform получает расширяемый provider layer: UCR можно использовать как managed
conference runtime, а другие video providers можно подключать адаптерами без
переписывания канонического Event domain. При этом UCR не становится обязательной
runtime-зависимостью и не получает business authority ClientPlatform.

Цена решения: для полноценного UCR webinar user flow нужен отдельный vertical slice,
который свяжет canonical registration/session с персональным single-use join redirect
и докажет live production readiness.

## Проверки

- pinned protobuf содержит все разрешённые request types/RPC;
- sidecar generated bindings проверяются против exact pinned UCR;
- conference mutation требует idempotency key;
- unknown method остаётся закрытым;
- provider-neutral registry не владеет business state;
- link-only unsupported writes fail-closed;
- cross-provider refs fail before network;
- UCR join fail-closed без browser gateway/WebRTC/TURN;
- attendance wrong-participant echo fail-closed;
- Canon, boundary, static/security, full CI and coverage gates remain green.

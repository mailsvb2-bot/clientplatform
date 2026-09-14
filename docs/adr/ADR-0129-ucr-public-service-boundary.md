# ADR-0129 — Universal Communication Runtime public service boundary

**Статус:** accepted  
**Дата:** 2026-09-14

## Контекст

Владелец отдельно поручил подключить `mailsvb2-bot/Universal-Communication-Runtime` (UCR) к ClientPlatform. Это является явным owner decision для межрепозиторной интеграции, но не отменяет Канон ClientPlatform: business/tenant/CRM/automation/payment/provider-delivery authority остаётся внутри ClientPlatform, а другой продукт не должен становиться скрытым вторым мозгом.

Первый ClientPlatform boundary был смержен PR #335 как disabled-by-default gateway client с exact UCR revision pin, secret reference, TLS guard, bounded I/O и fail-closed revision checking. После merge дополнительная сверка с реальным pinned UCR protobuf выявила контрактное расхождение: high-level операция `EnsureCommunicationContext` обещала Identity → Device → protected Group/Conversation → membership orchestration, хотя публичный `ucr.v1.IntegrationService` такого RPC не предоставляет. Аналогично короткий ClientPlatform `StartCall` request не содержал полного canonical UCR CallSession request и вынуждал бы будущий sidecar додумывать UCR-owned state.

Такой semantic adapter был бы неправильным даже при хороших security guards: чтобы реализовать обещание, gateway пришлось бы либо обращаться к внутренним UCR stores/core API, либо создавать собственные правила поверх UCR. Оба варианта размывают ownership и создают риск второго коммуникационного мозга.

## Решение

1. ClientPlatform интегрируется только с **публичными versioned UCR services** закреплённой exact revision.
2. HTTP gateway является transport adapter к публичному UCR API, а не semantic owner. Его `/v1/rpc` envelope содержит exact `service`, `method` и canonical request object.
3. Для pinned revision ClientPlatform разрешает только опубликованные методы `ucr.v1.IntegrationService`:
   - `SubmitCommand`;
   - `CreateIdentity`;
   - `LinkIdentity`;
   - `GetIdentity`;
   - `ResolveIdentityBinding`;
   - `CreateConversation`;
   - `GetConversation`;
   - `SendMessage`;
   - `GetMessage`;
   - `CreateCommunicationIntent`;
   - `GetCommunicationIntent`.
4. Для звонков разрешены только публичные методы `ucr.v1.CallService`: `StartCall`, `GetCall`, `SignalCall`.
5. ClientPlatform не публикует synthetic UCR operations для Device/Group/membership, пока соответствующего публичного UCR RPC нет в exact pinned revision.
6. `StartCall` и другие UCR RPC получают canonical request, подготовленный вызывающим application boundary; gateway не достраивает отсутствующие UCR fields и не создаёт новую call state machine.
7. Все mutating gateway RPC требуют business-operation idempotency key на ClientPlatform boundary. Это дополнительная retry-защита и не заменяет canonical UCR IDs/dedup/conflict semantics.
8. Read RPC не используют mutation idempotency header; попытка смешать семантики fail-closed.
9. Успешный gateway response обязан вернуть exact pinned UCR revision, exact service/method echo и object `result`. Mismatch fail-closed.
10. UCR module остаётся optional и disabled by default. Он не импортируется из общего `clientplatform.runtime` package init и не становится обязательной зависимостью для dependency-light architecture checks.
11. Provider credentials, ClientPlatform billing secrets и unrelated customer/profile data не копируются в UCR только ради routing. Передаётся только data, необходимая конкретному canonical UCR RPC.
12. UCR acknowledgement не превращается автоматически в provider delivery/read/media/business outcome evidence. Каждый такой факт принадлежит своему canonical owner.

## Revision pin

Текущий boundary закреплён на UCR SHA:

`8097b41e69634c944c225f7071e80b991d4ddc02`

Использовать `main`, branch name или moving tag запрещено. Upgrade требует exact SHA, protobuf/API diff review, ClientPlatform regression tests и green protected PR.

Текущий UCR `main` может быть новее закреплённой revision. Сам факт появления новой UCR phase не является основанием автоматически менять production/client contract.

## Deployment boundary

UCR reference repository содержит Tonic server builders/interoperability evidence, но это не означает автоматически существующий production listener. Production sidecar/listener, durable UCR store, service-principal credential provisioning, permission grants, quota policy, TLS/network placement, observability и rollout должны быть доказаны отдельным deployment slice.

Этот ADR разрешает архитектурную интеграцию по прямому решению владельца, но **не разрешает production deploy**. Production остаётся отдельной explicit owner-командой согласно Канону.

## Безопасность

- gateway disabled → no secret resolution / no network;
- exact revision check;
- HTTPS outside loopback;
- secret reference instead of raw token config;
- bounded request/response sizes and timeout;
- JSON finite-only (`NaN`/`Infinity` fail-closed);
- method whitelist before network I/O;
- mutation idempotency before network I/O;
- exact service/method response binding prevents confused-deputy/cross-RPC success;
- error detail is not echoed into application exception text;
- no ClientPlatform tenant/RBAC authority is delegated to UCR.

## Последствия

ClientPlatform получает честную и расширяемую точку подключения UCR без копирования его domain model и без обращения к внутреннему storage/core API. Sidecar можно реализовать как тонкий переводчик HTTP envelope → pinned UCR public gRPC services, а обновление UCR можно проверять как явный versioned contract change.

Цена решения — ClientPlatform не может обещать UCR capability, которой нет в public service surface. Device/group membership или другие будущие операции остаются недоступными до появления публичного UCR RPC либо отдельного owner-approved изменения архитектуры.

## Проверки

- exact IntegrationService whitelist совпадает с pinned protobuf;
- exact CallService whitelist совпадает с pinned protobuf;
- unknown `RegisterDevice`/другой method блокируется до network;
- mutation без idempotency key блокируется до network;
- read с mutation idempotency key блокируется;
- canonical call request пересылается без invented `tenant_key`/`participant_identity_keys` shortcut;
- invalid/non-finite request JSON fail-closed;
- response service/method/result mismatch fail-closed;
- revision mismatch, malformed response, auth rejection and missing secret remain fail-closed;
- feature disabled keeps existing Telegram/VK/MAX/email/SMS/web-chat behavior untouched.
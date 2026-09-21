# ADR-0131 — explicit UCR attendance pilot sync into canonical evidence

**Статус:** принято реализацией vertical slice  
**Дата:** 2026-09-21

## Контекст

ADR-0130 добавил строгий read-only adapter для `UCR GetParticipantAttendance`, но
намеренно не включал production activation и не записывал attendance автоматически в
ClientPlatform. Следующий шаг должен довести реальный source fact до уже существующих
Customer Timeline / feedback / value-proof контуров, не превращая UCR в второй CRM и
не давая observation права запускать бизнес-действия.

Существующий `ExternalProductConnector` был исторически webhook-oriented и требовал
HMAC credential. Использовать фиктивный webhook secret для pull-source было бы ложной
семантикой и случайно открыло бы trusted pull connector публичному webhook ingress.

## Решение

1. Тот же canonical `external_product_connectors` получает additive
   `ingress_mode=signed_webhook|trusted_pull`. Нового source/evidence store нет.
2. Все существующие connectors мигрируют как `signed_webhook`.
3. Public webhook ingress принимает только active `signed_webhook` connector.
   `trusted_pull` fail-closed отвергается до parsing/persistence.
4. Trusted pull использует реальную secret reference источника (для UCR — gateway
   token reference), но не выдаёт её за HMAC webhook secret.
5. Pilot sync вызывается только явно авторизованным application caller. Фонового
   scheduler/poller этот slice не добавляет.
6. До network read ClientPlatform требует active trusted-pull connector и уже
   существующий explicit customer binding. Pilot не создаёт и не переносит identity.
7. После network read binding и connector status повторно проверяются при persistence.
   Race с disable/rebind или новой observation revision закрывается fail-closed.
8. UCR echoed participant identity по-прежнему проверяется adapter-ом ADR-0130.
9. Нулевое attendance остаётся нулём и не создаёт «не пришёл» observation.
10. Повтор exact того же source snapshot не создаёт фиктивную новую revision:
    provenance fingerprint сравнивается с current canonical head.
11. Изменившийся snapshot создаёт exact next revision и supersedes current head.
12. Persistence идёт в существующий `external_product_event_receipts` как
    `event_type=evidence`; Customer Timeline и owner feedback работают без нового
    projection/store.
13. Pilot sync не создаёт Outcome, Sales event/stage, Next Best Action, AI job,
    follow-up, message send, consent или AutomationPolicy authorization.
14. `CLIENTPLATFORM_UCR_GATEWAY_ENABLED` остаётся disabled by default. Этот slice
    не включает production UCR credentials/listener и не активирует polling.

## Пользовательский результат

После явного pilot sync владелец видит в существующей карточке Customer подтверждённое
UCR-наблюдение, его источник/время/ограничения и может дать уже существующий feedback
«Полезно / Неверно / Не тот клиент». Это создаёт реальную выборку для value proof #388,
но ещё не меняет поведение продаж.

## Следующий gate

Влияние UCR observation на Growth/Sales/Next Best Action допускается только после
production source rollout, достаточного feedback sample и отдельного review policy /
consent boundary. До этого observation остаётся advisory evidence.

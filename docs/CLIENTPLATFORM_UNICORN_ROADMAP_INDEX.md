# ClientPlatform Unicorn Roadmap — navigation index

Полный исполнительный документ: [`CLIENTPLATFORM_UNICORN_ROADMAP.md`](CLIENTPLATFORM_UNICORN_ROADMAP.md).

Этот короткий index нужен для быстрой навигации; он не является отдельным источником истины.

## Start here

1. [`../AGENTS.md`](../AGENTS.md) — обязательный протокол работы для AI-чатов/агентов.
2. [`CLIENTPLATFORM_CANON_TZ.md`](CLIENTPLATFORM_CANON_TZ.md) — единственный нормативный Канон.
3. [`CLIENTPLATFORM_UNICORN_ROADMAP.md`](CLIENTPLATFORM_UNICORN_ROADMAP.md) — полный execution roadmap.

## Immediate queue

| Order | Slice | Status | Result |
|---:|---|---|---|
| 1 | U-001 Durable Outcome Ledger | NEXT | единый durable ledger реальных бизнес-outcomes |
| 2 | U-002 Acquisition Attribution Spine | QUEUED | источник привлечения проходит до customer/booking/order |
| 3 | U-003 Revenue Attribution & Unit Economics | QUEUED | revenue/CAC/ROAS только по доказуемой attribution |
| 4 | U-004 Yandex Campaign-level Read Analytics | QUEUED | campaign diagnostics отдельно от exact attribution |
| 5 | U-005 Managed Yandex Activation Policy | QUEUED | безопасный выход managed campaign из SERVING_OFF по consent/policy |
| 6 | U-006 Growth Cockpit | QUEUED | владелец видит лиды, деньги, рекламу и next actions в одном месте |
| 7 | U-007 Zero-to-First-Outcome Onboarding | QUEUED | новый бизнес быстро получает первый готовый action |
| 8 | U-008 CRM Lead Inbox | QUEUED | ни один acquisition lead не теряется |
| 9 | U-009 Follow-up Employee | QUEUED | безопасные follow-ups с stop/consent rules |
| 10 | U-010 Retention & Reactivation Engine | QUEUED | возврат старых клиентов с измеримым revenue outcome |

## Major milestones

- M1 — Measure Money.
- M2 — One Click to Customers.
- M3 — Never Lose a Lead.
- M4 — Revenue Operating System.
- M5 — Safe Autopilot.
- M6 — Omnichannel ClientPlatform.
- M7 — Full Telegram Mini App / Business Cockpit.
- M8 — Billing, Packaging and Monetization.
- M9 — Partner / Agency / Distribution Platform.
- M10 — Public API, Webhooks and Ecosystem.
- M11 — AI Operating Layer.
- M12 — Experimentation & Learning System.
- M13 — Privacy-safe intelligence moat.
- M14 — Vertical Packs.
- M15 — Trust, Compliance and Enterprise-grade Safety.
- M16 — Scale 10k → 100k businesses.
- M17 — International / provider portability.

## Rule

Не реализовывать milestone целиком одним PR. Если владелец не дал другую конкретную задачу, следующий чат берёт только текущий `NEXT`, делает минимальный полноценный vertical slice, тестирует, доводит PR до зелёного merge и только после этого переводит следующий `QUEUED` в `NEXT`.

# ADR-0121: Exact-AdId Yandex Direct growth analytics

## Status

Accepted for implementation on the ClientPlatform owner surface.

## Context

ClientPlatform already has tenant-scoped Yandex OAuth + PKCE, encrypted token storage, draft publication jobs, exact provider ad IDs, read-only budget evidence, and guarded spend controls. The missing product link is evidence-based performance reporting for the owner: provider spend/clicks must be connected to ClientPlatform promotion links and downstream customer outcomes without mixing unrelated ads in the same Yandex account.

## Decision

The owner analytics path is read-only and exact-ID scoped:

1. Select only active tenant-owned Yandex connections.
2. Select only `submitted` ClientPlatform publication jobs that have an `external_ad_id`.
3. Query Yandex Reports with `AD_PERFORMANCE_REPORT`, filtered by those exact `AdId` values.
4. Read `Impressions`, `Clicks` and `Cost` with `returnMoneyInMicros=true`, `IncludeVAT=NO`, and `IncludeDiscount=NO` to stay consistent with the existing spend evidence boundary.
5. Link each provider ad back to its persisted `promotion_campaign_id`.
6. Count unique local `opened` and `booked` customers from `promotion_events` and confirmed `won` sales leads for those customers.
7. Derive CTR, CPC, CPL, booking cost and CAC only when the corresponding denominator is non-zero.
8. Do not show revenue or ROMI until a payment amount is durably attributable to the same customer/source evidence. No synthetic revenue is allowed.

## Safety properties

- The analytics provider exposes Reports + OAuth refresh only; it exposes no ad/campaign mutation method.
- Unrelated Yandex ads are excluded from CPL/CAC because the request is filtered by exact ClientPlatform-created `AdId` values.
- Provider `201/202` means pending and never means zero spend.
- Provider identity mismatches fail closed.
- Token refresh is persisted through the existing encrypted credential vault.
- Tenant context and analytics permission are re-resolved before local reads.
- No budget, bidding, moderation, campaign state, payment provider or outbound customer message is mutated by this feature.

## Owner UX

The owner dashboard exposes `📊 Яндекс`. The screen supports 7-day and 30-day periods and links back to `📣 Рекламные кабинеты` and `✨ Получать клиентов`.

If there is no connected account or no exact tracked ClientPlatform ad, the UI states that explicitly and does not fabricate performance metrics.

# ADR-0073: Read-only Yandex Direct financial reconciliation

## Status

Accepted as the read-only provider boundary for Issue #74. No provider mutation or launch worker is enabled by this decision.

## Context

A stored owner consent is not sufficient to start advertising spend. Immediately before any future provider mutation, ClientPlatform must independently re-read campaign state, payment state, financial mode, available balance, strategy and actual spend.

Yandex Direct exposes campaign financial data through `Campaigns.get`. Monetary values are integers multiplied by 1,000,000. Daily actual cost is retrieved through the Reports service. Campaign balance excludes VAT, so the daily report used for reconciliation also excludes VAT.

A shared account exposes accumulated spend but does not provide a campaign-local available balance in the same response. Treating that value as available budget would be unsafe.

## Decision

ClientPlatform adds a read-only adapter with two independent evidence records:

1. `YandexCampaignBudgetReadout` from one exact-ID `Campaigns.get` request;
2. `YandexDailySpendReadout` from one exact-campaign, exact-date Reports request.

The Reports request uses:

- `CAMPAIGN_PERFORMANCE_REPORT`;
- a `CampaignId` filter;
- `Date`, `CampaignId`, `Cost` fields;
- `returnMoneyInMicros: true`;
- `IncludeVAT: NO`;
- skipped report header, column header and summary.

HTTP 201 and 202 are retryable pending states, not zero spend. Malformed, foreign-campaign, foreign-date, negative or non-integer rows fail closed. A valid empty report is the only representation of zero daily spend.

Reconciliation creates a short-lived `ProviderBudgetSnapshot` only when:

- campaign ID, currency and report date match;
- both reads are recent and not future-dated;
- the campaign uses `CAMPAIGN_FUNDS` and exposes `Balance`;
- monetary micros convert exactly to supported currency minor units;
- the campaign, payment and strategy fields are present.

`SHARED_ACCOUNT_FUNDS` is readable for diagnostics but cannot produce a launchable snapshot until a separate provider-derived available-balance contract exists.

## Consequences

This slice can prove current financial state but still cannot launch or stop advertising. A future runtime must:

- refresh both readouts immediately before every provider mutation;
- compare the fresh snapshot with the immutable consent receipt;
- reject changed campaign, regions, strategy, account, currency or limits;
- persist reconciliation evidence and enqueue an idempotent launch/stop command;
- enforce hard and daily caps continuously, accounting for reporting latency.

No draft-confirmation callback is spend consent, and no read failure may be interpreted as zero spend.

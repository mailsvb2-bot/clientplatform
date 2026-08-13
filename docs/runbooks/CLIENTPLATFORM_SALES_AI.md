# ClientPlatform Sales AI v3

Sales AI is a provider-neutral advisory layer over the canonical ClientPlatform sales core (#120). It analyzes opted-in managed-bot customer text, stores a strictly validated projection, can open the existing handoff path through the canonical orchestrator, grounds suggested offers/prices in ClientPlatform data, and can generate an owner-review draft. It never sends a message, marks payment, sets won/lost, bypasses ContactBasis, or owns the sales state machine.

## Providers

Supported adapters:

- `deepseek` — official DeepSeek Chat Completions endpoint (recommended/default);
- `openai` — official OpenAI Responses endpoint;
- `openai_compatible` — explicit HTTPS Chat-Completions-compatible endpoint.

The domain/application layer sees one provider interface; vendor formats stay inside `sales_ai_provider.py`.

## Production DeepSeek configuration

```dotenv
CLIENTPLATFORM_SALES_AI_ENABLED=1
CLIENTPLATFORM_SALES_AI_PROVIDER=deepseek
CLIENTPLATFORM_SALES_AI_MODEL=deepseek-v4-flash
CLIENTPLATFORM_SALES_AI_BASE_URL=https://api.deepseek.com
CLIENTPLATFORM_SALES_AI_API_KEY_REFERENCE=secret://env/CLIENTPLATFORM_SECRET_SALES_AI_DEEPSEEK_API_KEY
CLIENTPLATFORM_SECRET_SALES_AI_DEEPSEEK_API_KEY=<secret>
CLIENTPLATFORM_SALES_AI_ALLOW_CUSTOM_ENDPOINT=0
CLIENTPLATFORM_SALES_AI_TIMEOUT_SEC=20
CLIENTPLATFORM_SALES_AI_MAX_OUTPUT_TOKENS=900
CLIENTPLATFORM_SALES_AI_MAX_MESSAGE_CHARS=6000
CLIENTPLATFORM_SALES_AI_BATCH_SIZE=1
CLIENTPLATFORM_SALES_AI_INTERVAL_SEC=1.0
CLIENTPLATFORM_SALES_AI_LOCK_TTL_SEC=120
CLIENTPLATFORM_SALES_AI_MAX_ATTEMPTS=5
CLIENTPLATFORM_SALES_AI_RAW_MESSAGE_TTL_HOURS=168
CLIENTPLATFORM_SALES_AI_ANALYSIS_TTL_DAYS=90
```

The DeepSeek adapter uses `/chat/completions`, JSON output and explicitly disables thinking for this bounded extraction/drafting workload. ClientPlatform parses the returned JSON again with strict domain validation.

## Custom compatible endpoint

Custom endpoints are disabled by default and require both switches plus an exact host allowlist:

```dotenv
CLIENTPLATFORM_SALES_AI_PROVIDER=openai_compatible
CLIENTPLATFORM_SALES_AI_MODEL=<model-id>
CLIENTPLATFORM_SALES_AI_BASE_URL=https://provider.example/v1
CLIENTPLATFORM_SALES_AI_ALLOW_CUSTOM_ENDPOINT=1
CLIENTPLATFORM_SALES_AI_ALLOWED_HOSTS=provider.example
CLIENTPLATFORM_SALES_AI_API_KEY_REFERENCE=secret://env/CLIENTPLATFORM_SECRET_SALES_AI_API_KEY
CLIENTPLATFORM_SECRET_SALES_AI_API_KEY=<secret>
```

Runtime transport additionally resolves the destination and refuses localhost, link-local, private, loopback or otherwise non-global IP addresses. Do not allowlist a hostname that resolves to infrastructure metadata/private services.

## Consent and egress barrier

Cloud AI is fail-closed and requires:

1. server-wide AI runtime enabled;
2. a dedicated tenant consent row enabled by an authorized business member;
3. confirmed customer notice/processing basis;
4. non-`no_cloud` data mode;
5. consent target exactly equal to current `provider + base URL`.

Consent has a monotonic `consent_epoch`. Provider/endpoint changes invalidate old consent. Enabling/disabling AI increments the epoch and terminally cancels pending/retry/processing jobs.

At the actual provider boundary ClientPlatform starts a dedicated lock-holder thread with its own DB transaction, locks the exact processing job lease and the tenant consent row, prepares/redacts the text, and holds those DB row locks until the network request finishes. A concurrent disable/provider change must update the same consent row and therefore waits for an already-started request; after the toggle returns, later egress cannot validate the old target/epoch. The dedicated thread is intentional: production DB connections are reusable per worker thread, so the event-loop thread must not hold a long DB transaction across `await`.

## Data modes

- `redacted` — default; removes obvious direct identifiers (email, phone-like strings, long numeric identifiers, @handles and URLs) before cloud egress;
- `standard` — raw bounded text may be sent after explicit notice/consent;
- `no_cloud` — provider egress is denied.

For medical, psychological, legal and similarly sensitive businesses, use `no_cloud` unless the deployment has a separately reviewed lawful processing/redaction policy. Model-side `sensitive_context` detection is **not** treated as pre-egress privacy protection.

## Failure isolation

The Managed Bot Gateway always attempts its normal Telegram dispatcher path. Sales/AI capture is a side channel wrapped in fail-open-to-core error handling: invalid AI configuration, provider failure or AI DB failure is logged/degraded but does not stop the managed bot from processing the customer update. The deterministic sales signal/orchestrator is not conditional on AI consent.

The optional AI worker startup is also isolated from core application startup. Production deployment should still run the AI preflight as a strict release gate when Sales AI is enabled.

## Queue and ordering

The worker claims exactly one network job per tick. The exact job row is locked for provider egress, so another worker cannot reclaim it during the request. Newer source messages supersede pending/retry older jobs; stale in-flight results are discarded after freshness validation.

Latest AI evidence is kept in a dedicated `(business_id, lead_id)` projection keyed by sortable `source_order_key`, not by timestamp/UUID ordering. An older analysis can never replace a newer projection.

## Sales semantics

AI outputs observations, never an action kind or hard payment/sales state. Canonical #120 orchestration still creates the ActionPlan/handoff/commercial candidate. High-confidence structured observations may produce **advisory** semantic milestone hints (`need_captured`, `qualification_passed`) for UI/diagnostics, but v3 intentionally does not let AI mutate evidence-only funnel milestones by itself.

`recommended_offer_kind` is matched only against active commercial-ladder steps. If the step references an active offering, its price is read from active `business_offering_prices`. Draft generation receives only this verified snapshot. If no verified price exists, the model is explicitly forbidden to invent one.

## Retention

Raw `customer_message` payloads used for AI are redacted after `CLIENTPLATFORM_SALES_AI_RAW_MESSAGE_TTL_HOURS` (default 168 hours). AI analysis projection/event payloads are removed/redacted after `CLIENTPLATFORM_SALES_AI_ANALYSIS_TTL_DAYS` (default 90 days). Tenant privacy cleanup continues to classify AI jobs/heads/projections as erasable customer-linked data; consent configuration is business-owned retained configuration.

## Owner flow

The owner explicitly enables AI in `Получать клиентов`. Default UI opt-in uses `redacted` mode and records confirmation that customers were informed about external AI processing. A provider/endpoint change requires a new opt-in. `Черновик ответа` only works for the newest analysis and a current non-handoff/non-NOOP canonical plan. The draft is displayed for review; no automatic provider/messenger send exists in this patch series.

## Preflight

Before deployment/restart when AI is enabled:

```bash
python scripts/clientplatform_sales_ai_preflight.py --env-file /etc/clientplatform/clientplatform.env
```

Then run the repository's normal full regression/static/security/PostgreSQL gates and perform one staging provider call with non-production synthetic data.

# Production acceptance checklist

This checklist is the final gate after deploying a ClientPlatform release and before increasing live traffic.

## Automated gate

Run from the project root:

```bash
python scripts/production_acceptance.py
```

Expected result:

```text
PRODUCTION ACCEPTANCE: OK
```

The runner composes existing checks instead of creating another validation owner: compile, tests, production readiness, runtime observability, health/readiness, and configured messenger probes.

## Required manual live-flow checks

The automated gate does not impersonate real users or perform external writes. Before declaring a release operationally accepted, verify the canonical flows below.
1. Official Telegram/VK/MAX owner entry opens ClientPlatform and never an unrelated product flow.
2. A new owner can create a business workspace and return to it later.
3. Owner/member permissions remain tenant-scoped; another business cannot be opened through a forged callback.
4. Customer invite/onboarding creates the intended customer relationship and customer portal.
5. Booking creation, cancellation and reminder delivery work through the canonical ClientPlatform scheduler/outbox.
6. Program media delivery uses the ClientPlatform media gateway and records progress idempotently.
7. A business payment can be recorded/refunded once, with matching outcome evidence and no cross-tenant leakage.
8. VK/MAX outbound delivery is visible to the real recipient and durable outbox state reaches `sent` without dead-letter growth.

## Hard stop conditions

Do not increase traffic if any of these happens:

- the regression or static/security gate fails;
- `/readyz` is not `200`;
- a configured messenger preflight is red;
- the canonical dispatch runtime is missing, stale or accumulating dead letters;
- recent service logs contain a current traceback;
- tenant isolation, privacy export/erasure, booking idempotency or business-payment evidence fails;
- the deployed SHA differs from the approved release SHA.

Live provider checks are operational evidence, not permission to weaken repository or deployment guardrails.

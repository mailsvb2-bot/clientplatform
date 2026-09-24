# ClientPlatform production rollout — PR #430

Date: 2026-09-24

Application source before release carrier: `0b003a430ad3e15b3e97b37dd67b8fda474fb119`.

Scope:
- guarantee both `⬅️ Назад` and `🏠 В главное меню` on dense VK/MAX native panels;
- preserve the existing actionable sales-stage transitions;
- paginate dense direction lists so native keyboard limits remain valid;
- stabilize the callback acknowledgement chaos regression test without changing production acknowledgement ordering.

Pre-merge evidence:
- PR #430 exact head passed all 18 required workflows;
- production disk maintenance completed successfully on the dedicated ClientPlatform host;
- canonical production deploy remains responsible for pre-deploy backup, capacity pressure cleanup, readiness, external HTTPS checks and rollback.
